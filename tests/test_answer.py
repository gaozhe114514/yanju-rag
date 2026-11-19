# -*- coding: utf-8 -*-
"""问答链路测试：用假 provider，不联网、不加载模型。"""
import json
import os
import tempfile
import unittest

from api.gap import log_gap
from answer.gate import RefusalGate
from answer.pipeline import AnswerPipeline
from answer.prompt import format_hits
from answer.providers import ExtractiveProvider, ProviderError, parse_payload


class FakeSearcher:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, skip_rerank=False):
        self.skip_rerank = skip_rerank
        return list(self.hits)


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, messages, hits=None):
        self.calls.append(messages)
        return self.responses.pop(0)


class FailingProvider:
    def generate(self, messages, hits=None):
        raise ProviderError("boom")


def make_hits():
    from retrieval.chunk import Chunk
    from retrieval.search import SearchHit

    return [
        SearchHit(Chunk("2019-408-31", 31,
                        "虚拟地址 2050 1225H 对应的页目录号、页号分别是\nA. 081H、101H"),
                  0.92),
        SearchHit(Chunk("2019-408-32", 32, "下列动态分区分配算法中，最容易产生内存碎片的是"),
                  0.41),
    ]


def payload(answer="页目录号是 081H。", cid="2019-408-31",
            quote="虚拟地址 2050 1225H"):
    return json.dumps({"refused": False, "answer": answer,
                       "citations": [{"chunk_id": cid, "quote": quote}],
                       "reason": ""}, ensure_ascii=False)


class ParseTest(unittest.TestCase):
    def test_fenced_json(self):
        obj = parse_payload('```json\n{"refused": true, "answer": ""}\n```')
        self.assertTrue(obj["refused"])

    def test_extra_text(self):
        obj = parse_payload('好的，结果如下：{"refused": false, "answer": "x"}')
        self.assertFalse(obj["refused"])

    def test_bad_json(self):
        with self.assertRaises(ProviderError):
            parse_payload("没有 JSON")


class PipelineTest(unittest.TestCase):
    def test_valid_citation(self):
        hits = make_hits()
        p = FakeProvider([payload()])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("页目录号怎么算")
        self.assertFalse(res.refused)
        self.assertEqual(res.attempts, 1)
        self.assertEqual(res.citations[0]["chunk_id"], "2019-408-31")
        self.assertEqual(len(p.calls), 1)

    def test_no_hits_refuses_without_calling_provider(self):
        p = FakeProvider([])
        res = AnswerPipeline(FakeSearcher([]), p).answer("任意问题")
        self.assertTrue(res.refused)
        self.assertEqual(res.reason, "no_hits")
        self.assertEqual(p.calls, [])

    def test_low_score_gate(self):
        hits = make_hits()
        p = FakeProvider([])
        res = AnswerPipeline(FakeSearcher(hits), p,
                             gate=RefusalGate(min_score=0.95)).answer("问题")
        self.assertTrue(res.refused)
        self.assertEqual(res.reason, "low_score")
        self.assertEqual(p.calls, [])

    def test_bad_quote_retries_then_accepts(self):
        hits = make_hits()
        p = FakeProvider([payload(quote="这句话不在原文里"),
                          payload(quote="最容易产生内存碎片的是", cid="2019-408-32")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("内存碎片")
        self.assertFalse(res.refused)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(len(p.calls), 2)

    def test_bad_quote_twice_refuses(self):
        hits = make_hits()
        p = FakeProvider([payload(quote="编造的引用一"),
                          payload(quote="编造的引用二")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("页目录号")
        self.assertTrue(res.refused)
        self.assertTrue(res.reason.startswith("citation_failed"))
        self.assertEqual(res.attempts, 2)

    def test_unknown_chunk_id(self):
        hits = make_hits()
        p = FakeProvider([payload(cid="2019-408-99"),
                          payload(cid="2019-408-99")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("页目录号")
        self.assertTrue(res.refused)

    def test_quote_label_prefix_is_tolerated(self):
        hits = make_hits()
        p = FakeProvider([payload(quote="答案：虚拟地址 2050 1225H")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("页目录号")
        self.assertFalse(res.refused)

    def test_answer_letter_alone_is_too_short(self):
        hits = make_hits()
        p = FakeProvider([payload(quote="答案：D"), payload(quote="答案：D")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("这道题选什么")
        self.assertTrue(res.refused)
        self.assertIn("过短", res.reason)

    def test_whitespace_insensitive_quote(self):
        from retrieval.chunk import Chunk
        from retrieval.search import SearchHit
        hits = [SearchHit(Chunk("x", 1, "树转成二叉树后，后\n根遍历序列相同"), 0.9)]
        p = FakeProvider([payload(cid="x", quote="后根遍历序列相同")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("树的遍历")
        self.assertFalse(res.refused)

    def test_provider_error_refuses(self):
        res = AnswerPipeline(FakeSearcher(make_hits()), FailingProvider()).answer("问题")
        self.assertTrue(res.refused)
        self.assertTrue(res.reason.startswith("provider_error"))

    def test_refused_must_be_bool(self):
        hits = make_hits()
        raw = json.dumps({"refused": "false", "answer": "x", "citations": []},
                         ensure_ascii=False)
        p = FakeProvider([raw, raw])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("问题")
        self.assertTrue(res.refused)
        self.assertTrue(res.reason.startswith("citation_failed"))

    def test_model_refusal_is_passed_through(self):
        hits = make_hits()
        raw = json.dumps({"refused": True, "answer": "", "citations": [],
                          "reason": "资料只给了选项，没有解析"}, ensure_ascii=False)
        res = AnswerPipeline(FakeSearcher(hits), FakeProvider([raw])).answer("为什么")
        self.assertTrue(res.refused)
        self.assertIn("资料", res.reason)

    def test_extractive_provider_quotes_top_hit(self):
        hits = make_hits()
        res = AnswerPipeline(FakeSearcher(hits), ExtractiveProvider(),
                             retry=False).answer("页目录号")
        self.assertFalse(res.refused)
        self.assertEqual(res.citations[0]["chunk_id"], "2019-408-31")


class StyleTest(unittest.TestCase):
    """伙伴 / 人格 / 技能只改措辞，不能改事实与引用规则。"""

    def test_style_reaches_system_prompt(self):
        p = FakeProvider([payload()])
        AnswerPipeline(FakeSearcher(make_hits()), p).answer(
            "页目录号怎么算", "像同桌一样讲，语气轻松一点")
        system = p.calls[0][0]["content"]
        self.assertIn("像同桌一样讲", system)
        self.assertIn("不得改变事实", system)
        self.assertIn("不得放宽", system)

    def test_empty_style_leaves_prompt_unchanged(self):
        p = FakeProvider([payload()])
        AnswerPipeline(FakeSearcher(make_hits()), p).answer("页目录号")
        self.assertNotIn("【讲解方式】", p.calls[0][0]["content"])

    def test_style_survives_retry(self):
        p = FakeProvider([payload(quote="编造的引用一"), payload()])
        AnswerPipeline(FakeSearcher(make_hits()), p).answer("页目录号", "先给结论")
        self.assertIn("先给结论", p.calls[1][0]["content"])


class KnowledgePromptTest(unittest.TestCase):
    def _knowledge_hit(self):
        from retrieval.chunk import Chunk
        from retrieval.search import SearchHit
        ch = Chunk(
            "ds-quicksort", 0,
            "快速排序\n章节：排序\n快速排序每一趟选一个枢轴，平均时间复杂度 O(n log n)。\n"
            "易错点：快速排序不稳定。",
            subject="ds", kind="knowledge", title="快速排序", chapter="排序",
        )
        return SearchHit(ch, 0.88)

    def test_format_hits_marks_document_source(self):
        from retrieval.chunk import Chunk
        from retrieval.search import SearchHit
        hits = [SearchHit(Chunk("doc-1-001", 1, "快速排序的题",
                                kind="doc", title="排序笔记"), 0.5)]
        text = format_hits(hits)
        self.assertIn("来源=资料库", text)
        self.assertIn("文档=排序笔记", text)
        self.assertNotIn("类型=真题", text)
        self.assertNotIn("答案：", text)

    def test_knowledge_citation_verified(self):
        hits = [self._knowledge_hit()]
        p = FakeProvider([payload(cid="ds-quicksort",
                                  quote="平均时间复杂度 O(n log n)")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("快速排序时间复杂度")
        self.assertFalse(res.refused)
        self.assertEqual(res.citations[0]["chunk_id"], "ds-quicksort")

    def test_knowledge_quote_must_be_verbatim(self):
        hits = [self._knowledge_hit()]
        p = FakeProvider([payload(cid="ds-quicksort", quote="快速排序是稳定排序"),
                          payload(cid="ds-quicksort", quote="快速排序是稳定排序")])
        res = AnswerPipeline(FakeSearcher(hits), p).answer("快速排序稳定吗")
        self.assertTrue(res.refused)
        self.assertTrue(res.reason.startswith("citation_failed"))


class GapLogTest(unittest.TestCase):
    def test_append_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gap.jsonl")
            log_gap(path, "资料外的问题", "no_hits")
            log_gap(path, "第二个问题", "citation_failed")
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["query"], "资料外的问题")
        self.assertEqual(rows[1]["reason"], "citation_failed")


if __name__ == "__main__":
    unittest.main()
