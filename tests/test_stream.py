# -*- coding: utf-8 -*-
"""流式链路测试：增量解析 answer、SSE 编码、pipeline 的 reset/重试。"""
import json
import unittest

from answer.pipeline import AnswerPipeline
from answer.stream import AnswerStream, extract_answer, sse
from tests.test_answer import FakeSearcher, make_hits, payload


class ExtractTest(unittest.TestCase):
    def test_complete_answer(self):
        text, closed = extract_answer('{"refused": false, "answer": "页目录号是 081H。"}')
        self.assertTrue(closed)
        self.assertEqual(text, "页目录号是 081H。")

    def test_partial_answer(self):
        text, closed = extract_answer('{"answer": "先算页目录')
        self.assertFalse(closed)
        self.assertEqual(text, "先算页目录")

    def test_escapes_and_unicode(self):
        raw = json.dumps({"answer": '第一行\n"引号" \\ 反斜杠 中'}, ensure_ascii=False)
        text, closed = extract_answer(raw)
        self.assertTrue(closed)
        self.assertEqual(text, '第一行\n"引号" \\ 反斜杠 中')

    def test_escaped_unicode_split_across_chunks(self):
        stream = AnswerStream()
        chunks = ['{"answer": "\\u4e2', 'd\\u6587"}']
        out = "".join(stream.feed(c) for c in chunks)
        self.assertEqual(out, "中文")
        self.assertTrue(stream.closed)

    def test_no_key_yet(self):
        self.assertEqual(extract_answer('{"refused": false'), ("", False))

    def test_emits_each_piece_once(self):
        stream = AnswerStream()
        first = stream.feed('{"answer": "快速排序')
        second = stream.feed("平均 O(n log n)")
        third = stream.feed('", "citations": []}')
        self.assertEqual(first, "快速排序")
        self.assertEqual(second, "平均 O(n log n)")
        self.assertEqual(third, "")


class SSEEncodeTest(unittest.TestCase):
    def test_frame(self):
        frame = sse({"type": "delta", "text": "你好"})
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame[6:]), {"type": "delta", "text": "你好"})


class StreamProvider:
    """把一段 JSON 按固定长度切开流出来。"""

    def __init__(self, raw, size=7):
        self.raw = raw
        self.size = size
        self.calls = 0

    def generate_stream(self, messages, hits=None):
        self.calls += 1
        for i in range(0, len(self.raw), self.size):
            yield self.raw[i:i + self.size]


class StreamPipelineTest(unittest.TestCase):
    def collect(self, gen):
        return list(gen)

    def test_streams_answer_then_done(self):
        provider = StreamProvider(payload())
        pipe = AnswerPipeline(FakeSearcher(make_hits()), provider)
        events = self.collect(pipe.answer_stream("页目录号"))
        deltas = [e["text"] for e in events if e["type"] == "delta"]
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual("".join(deltas), "页目录号是 081H。")
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0]["result"].refused)
        self.assertEqual(done[0]["result"].citations[0]["chunk_id"], "2019-408-31")

    def test_citation_failure_resets_then_refuses(self):
        bad = json.dumps({"refused": False, "answer": "猜的",
                          "citations": [{"chunk_id": "2019-408-31",
                                         "quote": "资料里没有这句话"}],
                          "reason": ""}, ensure_ascii=False)
        provider = StreamProvider(bad, size=9)
        pipe = AnswerPipeline(FakeSearcher(make_hits()), provider)
        events = self.collect(pipe.answer_stream("页目录号"))
        self.assertEqual([e["type"] for e in events].count("reset"), 1)
        self.assertEqual(provider.calls, 2)
        done = [e for e in events if e["type"] == "done"][0]["result"]
        self.assertTrue(done.refused)
        self.assertTrue(done.reason.startswith("citation_failed"))

    def test_skip_rerank_reaches_searcher(self):
        seen = {}

        class S:
            reranker = object()

            def search(self, query, skip_rerank=False):
                seen["skip"] = skip_rerank
                return []

        pipe = AnswerPipeline(S(), StreamProvider(payload()))
        events = self.collect(pipe.answer_stream("今天开封市天气什么样",
                                                 skip_rerank=True))
        done = [e for e in events if e["type"] == "done"][0]["result"]
        self.assertTrue(seen["skip"])
        self.assertTrue(done.refused)
        self.assertEqual(done.timings.get("rerank_skipped"), 1)

    def test_gate_refusal_has_no_delta(self):
        class EmptySearcher:
            def search(self, query, skip_rerank=False):
                return []

        pipe = AnswerPipeline(EmptySearcher(), StreamProvider(payload()))
        events = self.collect(pipe.answer_stream("随便问问"))
        self.assertFalse([e for e in events if e["type"] == "delta"])
        self.assertTrue([e for e in events if e["type"] == "done"][0]["result"].refused)


if __name__ == "__main__":
    unittest.main()
