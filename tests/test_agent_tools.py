# -*- coding: utf-8 -*-
"""Agent 的工具边界、状态序列化、线程号与 checkpointer 选择。"""
import json
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from agent.checkpoint import build_checkpointer
from agent.prompt import refine_query
from agent.runtime import AgentRuntime, clean_thread_id
from agent.state import (hit_from_dict, hit_to_dict, stats_from_state,
                         stats_to_dict)
from agent.tools import SEARCH_KB, SEARCH_KB_SCHEMA, SearchKBTool
from answer.aggregate import TableStats
from retrieval.chunk import Chunk
from retrieval.search import SearchHit

CHUNKS = [
    Chunk(id="a", seq=1, text="第一段原文", subject="计算机网络"),
    Chunk(id="b", seq=2, text="第二段原文", subject="计算机网络"),
    Chunk(id="c", seq=3, text="第三段原文", subject="计算机网络"),
]


class SearchKBToolTest(unittest.TestCase):
    def test_tool_returns_top_k_and_keeps_chunk_objects(self):
        seen = []

        def search(query, skip_rerank=False):
            seen.append((query, skip_rerank))
            return [SearchHit(c, 0.5) for c in CHUNKS]

        tool = SearchKBTool(search, top_k=2)
        result = tool("三次握手", skip_rerank=False)
        self.assertEqual(result.name, SEARCH_KB)
        self.assertEqual([h.chunk.id for h in result.hits], ["a", "b"])
        self.assertEqual(seen, [("三次握手", False)])
        payload = result.call_payload()
        self.assertEqual(payload["args"]["query"], "三次握手")
        self.assertEqual(payload["hits"], 2)
        self.assertEqual(payload["chunk_ids"], ["a", "b"])

    def test_schema_is_json_schema_shaped(self):
        self.assertEqual(SEARCH_KB_SCHEMA["name"], SEARCH_KB)
        self.assertIn("query", SEARCH_KB_SCHEMA["parameters"]["properties"])
        self.assertEqual(SEARCH_KB_SCHEMA["parameters"]["required"], ["query"])


class StateSerializationTest(unittest.TestCase):
    def test_hit_roundtrip_keeps_text_and_score(self):
        hit = SearchHit(CHUNKS[0], 0.75, 1.5, 0.25)
        again = hit_from_dict(hit_to_dict(hit))
        self.assertEqual(again.chunk.id, "a")
        self.assertEqual(again.chunk.text, "第一段原文")
        self.assertAlmostEqual(again.score, 0.75)
        self.assertAlmostEqual(again.bm25, 1.5)
        self.assertAlmostEqual(again.vector, 0.25)

    def test_hit_roundtrip_is_json_serializable(self):
        blob = json.dumps(hit_to_dict(SearchHit(CHUNKS[1], 0.3)),
                          ensure_ascii=False)
        self.assertIn("第二段原文", blob)

    def test_broken_checkpoint_hit_is_ignored_by_runtime(self):
        self.assertRaises(ValueError, hit_from_dict, {"chunk": {"text": "x"}})

    def test_stats_roundtrip(self):
        stats = TableStats("共 30 行", [30], False)
        again = stats_from_state({"stats": stats_to_dict(stats)})
        self.assertEqual(again.text, "共 30 行")
        self.assertEqual(again.numbers, [30])
        self.assertFalse(again.partial)


class ThreadAndCheckpointTest(unittest.TestCase):
    def test_thread_id_is_sanitized(self):
        self.assertEqual(clean_thread_id("a b/../c"), "ab..c")
        self.assertEqual(clean_thread_id(""), "")
        self.assertEqual(len(clean_thread_id("x" * 100)), 64)

    def test_checkpointer_falls_back_to_memory_without_database(self):
        handle = build_checkpointer("")
        self.assertEqual(handle.kind, "memory")
        self.assertIsNotNone(handle.saver)
        handle.close()

    def test_runtime_exposes_checkpointer_kind(self):
        runtime = AgentRuntime(InMemorySaver(), "memory")
        self.assertEqual(runtime.kind, "memory")


class StreamingTest(unittest.TestCase):
    def test_stream_emits_tool_delta_reset_and_final(self):
        from tests.test_agent_graph import (HIT, INVALID, VALID, FakePlanner,
                                            FakeProvider, FakeSearch, deps)
        runtime = AgentRuntime(InMemorySaver(), "memory")
        events = list(runtime.stream(
            deps=deps(FakeSearch([HIT]),
                      FakeProvider([INVALID, VALID], refine="refined"),
                      planner=FakePlanner()),
            query="原问题", thread_id="s1", owner="user:1"))
        types = [e["type"] for e in events]
        self.assertIn("tool", types)
        self.assertIn("delta", types)
        self.assertIn("reset", types)  # 第一次引用没过，前端要清空已显示的文字
        finals = [e for e in events if e["type"] == "final"]
        self.assertEqual(len(finals), 1)
        result = finals[0]["result"]
        self.assertFalse(result.refused)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.thread_id, "s1")


class RefineFallbackTest(unittest.TestCase):
    def test_rule_refine_adds_top_title_when_no_model(self):
        chunk = Chunk(id="k1", seq=0, text="正文", kind="knowledge",
                      title="TCP 三次握手")
        query, source = refine_query(None, "为什么握手三次", ["引用不匹配"],
                                     [SearchHit(chunk, 0.8)])
        self.assertEqual(source, "rule")
        self.assertIn("TCP 三次握手", query)


if __name__ == "__main__":
    unittest.main()
