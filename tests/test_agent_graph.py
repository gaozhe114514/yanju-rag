# -*- coding: utf-8 -*-
"""Agent 状态图：规划、工具调用、拒答、换查询重试与最终拒答。"""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from langgraph.checkpoint.memory import InMemorySaver

from agent.nodes import AgentDeps
from agent.runtime import AgentRuntime
from answer.agent import AgentPlan
from answer.gate import RefusalGate
from answer.providers import ProviderError
from retrieval.chunk import Chunk
from retrieval.search import SearchHit

CHUNK = Chunk(
    id="2019-408-01",
    seq=1,
    text="TCP 建立连接需要三次握手，才能确认双方收发能力。",
    answer="A",
    analysis="三次握手确认双方都能收发。",
    subject="计算机网络",
)
HIT = SearchHit(CHUNK, 0.9)

VALID = json.dumps({
    "refused": False,
    "answer": "三次握手是为了确认双方的收发能力。",
    "citations": [{"chunk_id": "2019-408-01",
                   "quote": "TCP 建立连接需要三次握手"}],
    "reason": "",
}, ensure_ascii=False)

INVALID = json.dumps({
    "refused": False,
    "answer": "这句引用是编的。",
    "citations": [{"chunk_id": "2019-408-01",
                   "quote": "原文里根本没有这句话啊"}],
    "reason": "",
}, ensure_ascii=False)


class FakeSearch:
    def __init__(self, hits=None, by_query=None):
        self.hits = list(hits or [])
        self.by_query = dict(by_query or {})
        self.calls = []

    def __call__(self, query, skip_rerank=False):
        self.calls.append(query)
        return list(self.by_query.get(query, self.hits))


class FakeProvider:
    def __init__(self, outputs, refine="refined query", error=None):
        self.outputs = list(outputs)
        self.refine = refine
        self.error = error
        self.calls = []
        self.complete_calls = []

    def generate_stream(self, messages, hits=None):
        self.calls.append(messages)
        if self.error:
            raise self.error
        out = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        for i in range(0, len(out), 17):
            yield out[i:i + 17]

    def generate(self, messages, hits=None):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]

    def complete(self, messages, temperature=0.0, max_tokens=200,
                 json_mode=False):
        self.complete_calls.append(messages)
        return json.dumps({"query": self.refine, "reason": "test"},
                          ensure_ascii=False)


class FakePlanner:
    def __init__(self, intent="kb"):
        self.intent = intent
        self.histories = []

    def plan(self, query, history=None):
        self.histories.append(list(history or []))
        return AgentPlan(self.intent, query, query, "none", "", 0)


def deps(search, provider, planner=None, refiner=None, retry=True):
    return AgentDeps(
        search=search,
        prepare=lambda q, hits: (hits, None),
        provider=provider,
        gate=RefusalGate(0.0, 0.0),
        agent=planner,
        refiner=refiner,
        retry=retry,
    )


class AgentFlowTest(unittest.TestCase):
    def setUp(self):
        self.runtime = AgentRuntime(InMemorySaver(), "memory")

    def run_agent(self, **kwargs):
        return self.runtime.run(**kwargs)

    def test_successful_answer_has_tool_call_and_citations(self):
        search = FakeSearch([HIT])
        provider = FakeProvider([VALID])
        result = self.run_agent(
            deps=deps(search, provider), query="TCP 为什么要三次握手",
            thread_id="t1", owner="user:1")
        self.assertFalse(result.refused)
        self.assertEqual(result.answer, "三次握手是为了确认双方的收发能力。")
        self.assertEqual(result.citations[0]["chunk_id"], "2019-408-01")
        self.assertEqual(result.attempts, 1)
        self.assertEqual([c["name"] for c in result.tool_calls], ["search_kb"])
        self.assertEqual(search.calls, ["TCP 为什么要三次握手"])
        names = [s["name"] for s in result.steps]
        self.assertIn("理解问题", names)
        self.assertIn("工具调用 · search_kb", names)
        self.assertIn("拒答判断", names)
        self.assertIn("生成", names)
        self.assertIn("引用校验", names)

    def test_empty_hits_is_refused_before_generation(self):
        search = FakeSearch([])
        provider = FakeProvider([VALID])
        result = self.run_agent(
            deps=deps(search, provider), query="库外问题",
            thread_id="t1", owner="user:1")
        self.assertTrue(result.refused)
        self.assertEqual(result.reason, "no_hits")
        self.assertEqual(result.attempts, 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(result.tool_calls), 1)

    def test_citation_failure_retries_with_a_new_query(self):
        search = FakeSearch(by_query={
            "原问题": [HIT], "refined query": [HIT]})
        provider = FakeProvider([INVALID, VALID], refine="refined query")
        result = self.run_agent(
            deps=deps(search, provider), query="原问题",
            thread_id="t1", owner="user:1")
        self.assertFalse(result.refused)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(search.calls, ["原问题", "refined query"])
        self.assertEqual(len(result.tool_calls), 2)
        # 第二次生成拿到的是重试提示与新的检索词
        retry_prompt = provider.calls[1][-1]["content"]
        self.assertIn("没有通过引用校验", retry_prompt)
        self.assertIn("refined query", provider.calls[1][1]["content"])

    def test_second_citation_failure_is_refused(self):
        search = FakeSearch([HIT])
        provider = FakeProvider([INVALID, INVALID])
        result = self.run_agent(
            deps=deps(search, provider), query="原问题",
            thread_id="t1", owner="user:1")
        self.assertTrue(result.refused)
        self.assertTrue(result.reason.startswith("citation_failed:"))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.tool_calls), 2)

    def test_retry_search_with_no_hits_keeps_attempt_count(self):
        search = FakeSearch(by_query={"原问题": [HIT], "refined query": []})
        provider = FakeProvider([INVALID], refine="refined query")
        result = self.run_agent(
            deps=deps(search, provider), query="原问题",
            thread_id="t1", owner="user:1")
        self.assertTrue(result.refused)
        self.assertEqual(result.reason, "no_hits")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(search.calls, ["原问题", "refined query"])

    def test_model_refusal_is_kept(self):
        output = json.dumps({"refused": True, "answer": "", "citations": [],
                             "reason": "资料里没有覆盖这个问题"},
                            ensure_ascii=False)
        result = self.run_agent(
            deps=deps(FakeSearch([HIT]), FakeProvider([output])),
            query="库外问题", thread_id="t1", owner="user:1")
        self.assertTrue(result.refused)
        self.assertEqual(result.reason, "资料里没有覆盖这个问题")
        self.assertEqual(result.attempts, 1)

    def test_provider_error_is_refused_without_crashing(self):
        provider = FakeProvider([VALID], error=ProviderError("boom"))
        result = self.run_agent(
            deps=deps(FakeSearch([HIT]), provider), query="原问题",
            thread_id="t1", owner="user:1")
        self.assertTrue(result.refused)
        self.assertTrue(result.reason.startswith("provider_error:"))
        self.assertEqual(result.attempts, 1)

    def test_chitchat_does_not_search(self):
        search = FakeSearch([HIT])
        provider = FakeProvider([VALID])
        result = self.run_agent(
            deps=deps(search, provider, planner=FakePlanner("chitchat")),
            query="你好", thread_id="t1", owner="user:1")
        self.assertFalse(result.refused)
        self.assertEqual(result.intent, "chitchat")
        self.assertTrue(result.answer)
        self.assertEqual(search.calls, [])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result.tool_calls, [])

    def test_clarify_does_not_search(self):
        search = FakeSearch([HIT])
        provider = FakeProvider([VALID])
        result = self.run_agent(
            deps=deps(search, provider, planner=FakePlanner("clarify")),
            query="它为什么这样", thread_id="t1", owner="user:1")
        self.assertFalse(result.refused)
        self.assertEqual(result.intent, "clarify")
        self.assertIn("不确定", result.answer)
        self.assertEqual(search.calls, [])


class AgentMemoryTest(unittest.TestCase):
    def setUp(self):
        self.runtime = AgentRuntime(InMemorySaver(), "memory")

    def test_same_thread_sees_previous_turn(self):
        planner = FakePlanner()
        provider = FakeProvider([VALID])
        d = deps(FakeSearch([HIT]), provider, planner=planner)
        first = self.runtime.run(deps=d, query="第一问", thread_id="same",
                                 owner="user:1")
        second = self.runtime.run(deps=d, query="第二问", thread_id="same",
                                  owner="user:1")
        self.assertEqual(first.thread_id, "same")
        self.assertEqual(second.thread_id, "same")
        self.assertEqual(planner.histories[0], [])
        self.assertEqual([m["role"] for m in planner.histories[1]],
                         ["user", "assistant"])
        self.assertEqual(planner.histories[1][0]["content"], "第一问")

    def test_another_owner_cannot_continue_the_thread(self):
        planner = FakePlanner()
        provider = FakeProvider([VALID])
        d = deps(FakeSearch([HIT]), provider, planner=planner)
        self.runtime.run(deps=d, query="老板的问题", thread_id="shared",
                         owner="user:boss")
        result = self.runtime.run(deps=d, query="员工的问题", thread_id="shared",
                                  owner="user:staff")
        self.assertTrue(result.thread_reset)
        self.assertNotEqual(result.thread_id, "shared")
        self.assertEqual(planner.histories[1], [])  # 没读到老板的对话

    def test_new_thread_can_be_seeded_with_request_history(self):
        planner = FakePlanner()
        d = deps(FakeSearch([HIT]), FakeProvider([VALID]), planner=planner)
        self.runtime.run(
            deps=d, query="接着问", thread_id="fresh", owner="user:1",
            history=[{"role": "user", "content": "上一问"},
                     {"role": "assistant", "content": "上一答"}])
        self.assertEqual([m["content"] for m in planner.histories[0]],
                         ["上一问", "上一答"])


class AgentConcurrencySmokeTest(unittest.TestCase):
    def test_runtime_is_usable_from_a_worker_thread(self):
        runtime = AgentRuntime(InMemorySaver(), "memory")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                runtime.run, deps(FakeSearch([HIT]), FakeProvider([VALID])),
                "原问题", "t1", "user:1")
            result = future.result(timeout=30)
        self.assertFalse(result.refused)


if __name__ == "__main__":
    unittest.main()
