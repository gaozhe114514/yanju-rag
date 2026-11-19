# -*- coding: utf-8 -*-
"""检索层测试：不加载真实模型。"""
import unittest

import numpy as np

from retrieval.bm25 import BM25
from retrieval.chunk import Chunk
from retrieval.fuse import rrf
from retrieval.rerank import LexicalReranker
from retrieval.search import HybridSearcher, weak_evidence
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
    def test_doc_chunk_defaults(self):
        c = Chunk("doc-1-001", 1, "一段资料原文")
        self.assertEqual(c.kind, "doc")
        self.assertEqual(c.title, "")
        self.assertEqual(c.chapter, "")
        self.assertEqual(c.to_dict()["text"], "一段资料原文")


class FakeEmbedder:
    """按关键词计数造 3 维向量，只验证链路。"""

    def embed(self, texts, is_query=False):
        rows = []
        for t in texts:
            rows.append([t.count("二叉"), t.count("哈希"), t.count("排序") + 1.0])
        return np.asarray(rows, dtype=np.float32)


def sample_chunks():
    return [
        Chunk("doc-1-001", 1, "二叉排序树 中序遍历 递增"),
        Chunk("doc-2-001", 1, "哈希表 散列冲突 线性探测"),
        Chunk("doc-3-001", 1, "快速排序 时间复杂度"),
    ]


class SearcherTest(unittest.TestCase):
    def test_bm25_only(self):
        s = HybridSearcher(sample_chunks())
        hits = s.search("二叉排序树")
        self.assertEqual(hits[0].chunk.id, "doc-1-001")

    def test_hybrid_with_embedder(self):
        s = HybridSearcher(sample_chunks(), embedder=FakeEmbedder())
        hits = s.search("哈希冲突")
        self.assertEqual(hits[0].chunk.id, "doc-2-001")

    def test_external_vector_search(self):
        """传了 vector_search 就不再建内存向量索引，结果按 id 映射回来。"""
        chunks = sample_chunks()
        calls = []

        def fake_search(qv, top_k):
            calls.append(top_k)
            return [(chunks[2].id, 0.9), ("not-in-corpus", 0.5)]

        s = HybridSearcher(chunks, embedder=FakeEmbedder(),
                           vector_search=fake_search)
        self.assertIsNone(s.vector_index)
        hits = s.search("快速排序")
        self.assertTrue(calls)
        self.assertIn(chunks[2].id, [h.chunk.id for h in hits])

    def test_reranker_keeps_top_k(self):
        chunks = sample_chunks()
        s = HybridSearcher(chunks, reranker=LexicalReranker([c.text for c in chunks]),
                           k_final=2)
        hits = s.search("快速排序")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].chunk.id, "doc-3-001")

    def test_empty_corpus_is_rejected(self):
        with self.assertRaises(ValueError):
            HybridSearcher([])


class CountingReranker:
    def __init__(self):
        self.calls = 0

    def rerank(self, query, candidates, top_k=5):
        self.calls += 1
        return [(i, 0.9) for i, _ in candidates][:top_k]


class WeakEvidenceTest(unittest.TestCase):
    def test_bm25_alone_is_enough(self):
        self.assertFalse(weak_evidence([(0, 6.4)], {0: 0.4}))

    def test_vector_alone_is_enough(self):
        self.assertFalse(weak_evidence([(0, 0.0)], {0: 0.60}))

    def test_nothing_to_go_on(self):
        self.assertTrue(weak_evidence([(0, 3.9)], {0: 0.42}))
        self.assertTrue(weak_evidence([], {}))


class SkipRerankTest(unittest.TestCase):
    """实时数据问题：证据太弱就直接空手回去，不花精排那几秒。"""

    def test_skips_when_evidence_is_weak(self):
        chunks = sample_chunks()
        rr = CountingReranker()
        s = HybridSearcher(chunks, reranker=rr)
        self.assertEqual(s.search("今天开封市天气什么样", skip_rerank=True), [])
        self.assertEqual(rr.calls, 0)

    def test_reranks_when_evidence_is_strong(self):
        chunks = sample_chunks()
        rr = CountingReranker()
        s = HybridSearcher(chunks, reranker=rr)
        hits = s.search("快速排序 时间复杂度", skip_rerank=True)
        self.assertTrue(hits)
        self.assertEqual(rr.calls, 1)

    def test_default_never_skips(self):
        chunks = sample_chunks()
        rr = CountingReranker()
        s = HybridSearcher(chunks, reranker=rr)
        s.search("今天开封市天气什么样")
        self.assertEqual(rr.calls, 1)


if __name__ == "__main__":
    unittest.main()
