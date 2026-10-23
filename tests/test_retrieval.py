# -*- coding: utf-8 -*-
"""检索层测试：不加载真实模型。"""
import json
import os
import tempfile
import unittest

import numpy as np

from retrieval.bm25 import BM25
from retrieval.chunk import (Chunk, build_chunk, build_knowledge_chunk,
                             load_knowledge_chunks)
from retrieval.eval import evaluate
from retrieval.fuse import rrf
from retrieval.rerank import LexicalReranker
from retrieval.search import HybridSearcher, match_exact_question
from retrieval.tokenize import tokenize


class TokenizeTest(unittest.TestCase):
    def test_chinese_bigrams(self):
        self.assertEqual(tokenize("二叉排序树"), ["二叉", "叉排", "排序", "序树"])

    def test_mixed_terms(self):
        toks = tokenize("Cache 32KB，采用 LRU")
        self.assertIn("cache", toks)
        self.assertIn("32kb", toks)
        self.assertIn("lru", toks)


class BM25Test(unittest.TestCase):
    def test_rare_term_wins(self):
        docs = [tokenize("二叉排序树 中序遍历"), tokenize("哈希表 散列冲突 线性探测")]
        bm = BM25(docs)
        hits = bm.search(tokenize("线性探测"), top_k=2)
        self.assertEqual(hits[0][0], 1)


class RRTest(unittest.TestCase):
    def test_rank_fusion(self):
        a = [(0, 10.0), (1, 9.0)]
        b = [(1, 0.9), (0, 0.8)]
        fused = rrf([a, b], k=60, top_k=2)
        self.assertEqual([i for i, _ in fused], [0, 1])
        self.assertAlmostEqual(fused[0][1], 1 / 61 + 1 / 62)


class ChunkTest(unittest.TestCase):
    def test_retrieval_text_excludes_answer_and_analysis(self):
        q = {"id": "2019-408-01", "seq": 1, "stem": "题干内容",
             "options": {"A": "甲", "B": "乙"}, "answer": "A",
             "analysis": "这段解析不该进召回文本", "subject": "ds",
             "kp_ids": ["x"], "source_ref": {"file": "f", "line": 3}}
        c = build_chunk(q)
        self.assertEqual(c.text, "题干内容\nA. 甲\nB. 乙")
        self.assertNotIn("解析", c.text)
        self.assertEqual(c.answer, "A")
        self.assertEqual(c.analysis, "这段解析不该进召回文本")

    def test_question_chunk_defaults(self):
        c = Chunk("2019-408-01", 1, "题干")
        self.assertEqual(c.kind, "question")
        self.assertEqual(c.title, "")
        self.assertEqual(c.chapter, "")

    def test_knowledge_chunk_text_and_meta(self):
        card = {
            "id": "ds-quicksort", "subject": "ds", "chapter": "排序",
            "title": "快速排序", "body": "快速排序平均时间复杂度 O(n log n)。",
            "formulas": ["最坏 T(n)=O(n²)"], "pitfalls": "快速排序不稳定。",
        }
        c = build_knowledge_chunk(card, "data/curated/knowledge.jsonl", 8)
        self.assertEqual(c.kind, "knowledge")
        self.assertEqual(c.title, "快速排序")
        self.assertEqual(c.chapter, "排序")
        self.assertIn("快速排序平均时间复杂度 O(n log n)。", c.text)
        self.assertIn("关键公式：最坏 T(n)=O(n²)", c.text)
        self.assertIn("易错点：快速排序不稳定。", c.text)
        self.assertEqual(c.source_ref["file"], "data/curated/knowledge.jsonl")
        self.assertEqual(c.source_ref["line"], 8)
        self.assertEqual(c.answer, "")

    def test_load_knowledge_chunks(self):
        rows = [
            {"id": "ds-a", "subject": "ds", "chapter": "排序", "title": "A",
             "body": "正文 A"},
            {"id": "ds-b", "subject": "ds", "chapter": "排序", "title": "B",
             "body": "正文 B"},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "knowledge.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            chunks = load_knowledge_chunks(path)
        self.assertEqual([c.id for c in chunks], ["ds-a", "ds-b"])
        self.assertTrue(all(c.kind == "knowledge" for c in chunks))


class FakeEmbedder:
    """按关键词计数造 3 维向量，只验证链路。"""

    def embed(self, texts, is_query=False):
        rows = []
        for t in texts:
            rows.append([t.count("二叉"), t.count("哈希"), t.count("排序") + 1.0])
        return np.asarray(rows, dtype=np.float32)


def sample_chunks():
    return [
        Chunk("2019-408-01", 1, "二叉排序树 中序遍历 递增"),
        Chunk("2019-408-02", 2, "哈希表 散列冲突 线性探测"),
        Chunk("2019-408-03", 3, "快速排序 时间复杂度"),
    ]


class ExactQuestionTest(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            Chunk("2019-408-10", 10, "2019 年第十题题干"),
            Chunk("2020-408-10", 10, "2020 年第十题题干"),
            Chunk("2019-408-11", 11, "2019 年第十一题题干"),
        ]

    def test_raw_id(self):
        c = match_exact_question("看看 2019-408-10 的解析", self.chunks)
        self.assertEqual(c.id, "2019-408-10")

    def test_year_and_seq(self):
        c = match_exact_question("2019 年第 10 题答案是什么", self.chunks)
        self.assertEqual(c.id, "2019-408-10")

    def test_yearless_seq_is_ambiguous(self):
        self.assertIsNone(match_exact_question("第 10 题答案是什么", self.chunks))

    def test_missing_number(self):
        self.assertIsNone(match_exact_question("快速排序的时间复杂度", self.chunks))

    def test_searcher_pins_exact_question_first(self):
        s = HybridSearcher(self.chunks)
        hits = s.search("2019 年第 11 题")
        self.assertEqual(hits[0].chunk.id, "2019-408-11")
        self.assertEqual(hits[0].score, 1.0)

    def test_unknown_year_returns_no_hits(self):
        s = HybridSearcher(self.chunks)
        self.assertEqual(s.search("2024 年 408 第 1 题的答案是什么"), [])

    def test_unknown_id_returns_no_hits(self):
        s = HybridSearcher(self.chunks)
        self.assertEqual(s.search("2024-408-10 的解析"), [])

    def test_unknown_seq_returns_no_hits(self):
        s = HybridSearcher(self.chunks)
        self.assertEqual(s.search("2019 年第 99 题"), [])

    def test_yearless_seq_ambiguous_returns_no_hits(self):
        s = HybridSearcher(self.chunks)
        self.assertEqual(s.search("第 10 题答案是什么"), [])

    def test_yearless_seq_unique_is_pinned(self):
        s = HybridSearcher([Chunk("2019-408-10", 10, "2019 年第十题题干")])
        hits = s.search("第 10 题")
        self.assertEqual(hits[0].chunk.id, "2019-408-10")


class SearcherTest(unittest.TestCase):
    def test_bm25_only(self):
        s = HybridSearcher(sample_chunks())
        hits = s.search("二叉排序树")
        self.assertEqual(hits[0].chunk.id, "2019-408-01")

    def test_hybrid_with_embedder(self):
        s = HybridSearcher(sample_chunks(), embedder=FakeEmbedder())
        hits = s.search("哈希冲突")
        self.assertEqual(hits[0].chunk.id, "2019-408-02")

    def test_reranker_keeps_top_k(self):
        chunks = sample_chunks()
        s = HybridSearcher(chunks, reranker=LexicalReranker([c.text for c in chunks]),
                           k_final=2)
        hits = s.search("快速排序")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].chunk.id, "2019-408-03")


class EvalTest(unittest.TestCase):
    def test_metrics(self):
        chunks = sample_chunks()
        s = HybridSearcher(chunks)
        res = evaluate(s, [{"query": "二叉排序树", "gold": "2019-408-01"},
                           {"query": "不存在的词", "gold": "2019-408-02"}], k=3)
        self.assertEqual(res["n"], 2)
        self.assertAlmostEqual(res["recall@1"], 0.5)
        self.assertAlmostEqual(res["mrr"], 0.5)


if __name__ == "__main__":
    unittest.main()
