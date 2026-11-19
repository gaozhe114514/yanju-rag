# -*- coding: utf-8 -*-
"""统一 Agent 的路由：普通对话、知识库、工具。"""
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from agent.nodes import AgentDeps
from agent.route import RouteDecision, route_query
from agent.runtime import AgentRuntime
from answer.gate import RefusalGate
from retrieval.chunk import Chunk
from retrieval.search import SearchHit

CHUNK = Chunk(id="doc-1", seq=1, text="公司报销流程：先在系统提交申请，再由主管审批。",
              kind="doc", title="报销流程")
HIT = SearchHit(CHUNK, 0.8)


class RouteRulesTest(unittest.TestCase):
    def test_emotional_chat_goes_to_chat(self):
        d = route_query("我不想上班，上班好累", [], "", None,
                        ["weather", "time", "calc", "search", "fetch"])
        self.assertEqual(d.route, "chat")

    def test_weather_goes_to_tools(self):
        d = route_query("今天郑州天气怎么样", [], "", None,
                        ["weather", "time", "calc"])
        self.assertEqual(d.route, "tools")
        self.assertIn("weather", d.tools)

    def test_company_policy_goes_to_kb(self):
        d = route_query("公司报销流程是什么", [], "", None,
                        ["weather", "time", "calc"])
        self.assertEqual(d.route, "kb")

    def test_official_site_question_goes_to_search(self):
        d = route_query("你能从官网查到吗", [], "", None,
                        ["search", "fetch"])
        self.assertEqual(d.route, "tools")
        self.assertIn("search", d.tools)


class UnifiedGraphTest(unittest.TestCase):
    def setUp(self):
        self.runtime = AgentRuntime(InMemorySaver(), "memory")

    def make_deps(self, route, answer, tools=None, seen=None,
                  enabled=None, tool_calls=None):
        def chat_stream(query, history, style, tool_text=""):
            if seen is not None:
                seen.append(tool_text)
            yield answer

        def run_tools(names, query, study):
            if tool_calls is not None:
                tool_calls.append((list(names), query))
            return [{"name": "weather", "label": "天气", "ok": True,
                     "source": "wttr.in", "detail": "郑州现在 12℃。"}]

        return AgentDeps(
            search=lambda q, skip_rerank=False: [HIT],
            prepare=lambda q, hits: (hits, None),
            provider=object(),
            gate=RefusalGate(0, 0),
            router=lambda q, h, c: RouteDecision(route, tools or [], "test"),
            chat_stream=chat_stream,
            chat_reply=lambda q, h, s, tt="": answer,
            run_tools_fn=run_tools,
            tool_context_fn=lambda results: "天气：郑州现在 12℃。",
            tools=list(enabled or []),
        )

    def test_chat_route_has_no_citations(self):
        res = self.runtime.run(self.make_deps("chat", "别太累了，先休息一下。"),
                               query="我不想上班，上班好累", thread_id="c1",
                               owner="user:1")
        self.assertFalse(res.refused)
        self.assertEqual(res.route, "chat")
        self.assertEqual(res.answer, "别太累了，先休息一下。")
        self.assertEqual(res.citations, [])

    def test_tools_route_runs_tools_then_general_answer(self):
        seen = []
        res = self.runtime.run(
            self.make_deps("tools", "郑州现在 12℃。", ["weather"], seen),
            query="今天郑州天气怎么样", thread_id="t1", owner="user:1",
            tools=["weather"])
        self.assertFalse(res.refused)
        self.assertEqual(res.route, "tools")
        self.assertEqual(res.tools[0]["name"], "weather")
        self.assertIn("12℃", res.answer)
        self.assertIn("郑州现在 12℃", seen[0])

    def test_official_site_followup_fetches_url_from_history(self):
        calls = []
        deps = self.make_deps("tools", "官网首页介绍……", ["search"], enabled=[
            "search", "fetch"], tool_calls=calls)
        res = self.runtime.run(
            deps, query="你能从官网查到吗", thread_id="t2", owner="user:1",
            history=[
                {"role": "user", "content": "泛微网络是什么类型的公司"},
                {"role": "assistant",
                 "content": "可以看官网 www.weaver.com.cn 的产品介绍。"},
            ],
            tools=["search", "fetch"])
        self.assertFalse(res.refused)
        self.assertEqual(calls[0][0], ["fetch"])
        self.assertEqual(calls[0][1], "https://www.weaver.com.cn")


if __name__ == "__main__":
    unittest.main()
