# -*- coding: utf-8 -*-
"""双路召回 → RRF → 精排。"""
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .bm25 import BM25
from .chunk import Chunk
from .fuse import rrf
from .rerank import CrossEncoderReranker, LexicalReranker
from .tokenize import tokenize
from .vector import BgeEmbedder, VectorIndex


@dataclass
class SearchHit:
    chunk: Chunk
    score: float
    bm25: float = 0.0
    vector: float = 0.0


_EXACT_ID_RE = re.compile(r"(?<!\d)(20\d{2})\s*-\s*408\s*-\s*(\d{1,2})(?!\d)")
_EXACT_SEQ_RE = re.compile(
    r"(?:(20\d{2})\s*年\s*(?:408\s*)?)?第\s*(\d{1,2})\s*题")


def parse_exact_ref(query: str):
    """解析显式题号，返回 (year, seq)；year 为 None 表示只写了「第 N 题」。"""
    text = query or ""
    m = _EXACT_ID_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EXACT_SEQ_RE.search(text)
    if m:
        return (int(m.group(1)) if m.group(1) else None), int(m.group(2))
    return None


def resolve_exact_question(query: str, chunks: Sequence[Chunk]):
    """把「2019 年第 10 题」「2019-408-10」映射到题目 chunk。

    返回 (status, chunk)：found / missing / ambiguous / none。
    missing 和 ambiguous 必须分开：前者是资料里确实没有这道题，
    后者是同一题号对应多个年份，两者都不能随便挑一题顶上。
    """
    ref = parse_exact_ref(query)
    if ref is None:
        return "none", None
    year, seq = ref
    if year is not None:
        want = "%s-408-%02d" % (year, seq)
        for c in chunks:
            if c.kind == "question" and c.id == want:
                return "found", c
        return "missing", None
    candidates = [c for c in chunks if c.kind == "question" and c.seq == seq]
    if len(candidates) == 1:
        return "found", candidates[0]
    return ("ambiguous" if candidates else "missing"), None


def match_exact_question(query: str, chunks: Sequence[Chunk]) -> Optional[Chunk]:
    status, chunk = resolve_exact_question(query, chunks)
    return chunk if status == "found" else None


class HybridSearcher:
    def __init__(self, chunks: Sequence[Chunk], embedder=None,
                 reranker=None, k_each: int = 50, k_fuse: int = 30,
                 k_final: int = 5):
        if not chunks:
            raise ValueError("chunks 为空")
        self.chunks = list(chunks)
        self.k_each, self.k_fuse, self.k_final = k_each, k_fuse, k_final
        self.bm25 = BM25([tokenize(c.text) for c in self.chunks])
        self.embedder = embedder
        self.reranker = reranker
        self.vector_index: Optional[VectorIndex] = None
        if embedder is not None:
            vecs = embedder.embed([c.text for c in self.chunks])
            self.vector_index = VectorIndex(vecs)

    def search(self, query: str) -> List[SearchHit]:
        status, exact = resolve_exact_question(query, self.chunks)
        if status in ("missing", "ambiguous"):
            # 用户点名了题号，但库里要么没有、要么对不上年份：
            # 用别的题作答等于答非所问，直接返回空让拒答闸门拦下。
            return []

        bm_hits = self.bm25.search(tokenize(query), self.k_each)
        bmap = dict(bm_hits)
        lists = [bm_hits]
        vmap = {}
        if self.vector_index is not None and self.embedder is not None:
            qv = self.embedder.embed([query], is_query=True)[0]
            v_hits = self.vector_index.search(qv, self.k_each)
            vmap = dict(v_hits)
            lists.append(v_hits)

        fused = rrf(lists, k=60, top_k=self.k_fuse)
        ranked = (self.reranker.rerank(query, fused, self.k_final)
                  if self.reranker is not None else fused[:self.k_final])
        hits = [SearchHit(self.chunks[i], s, bmap.get(i, 0.0), vmap.get(i, 0.0))
                for i, s in ranked]

        if exact is None:
            return hits
        # 显式题号优先：排第一并给 1.0 分，保证拒答闸门不会把用户点名的题拦掉
        return ([SearchHit(exact, 1.0)] +
                [h for h in hits if h.chunk.id != exact.id][:max(0, self.k_final - 1)])


MODES = ("bm25", "hybrid", "hybrid-rerank", "hybrid-ce")


def build_searcher(chunks: Sequence[Chunk], mode: str = "hybrid",
                   model_name: str = "BAAI/bge-small-zh-v1.5",
                   reranker_model: str = "BAAI/bge-reranker-v2-m3",
                   k_fuse: int = 30, k_final: int = 5) -> HybridSearcher:
    if mode not in MODES:
        raise ValueError("未知 mode: %s" % mode)
    embedder = None
    reranker = None
    if mode in ("hybrid", "hybrid-rerank", "hybrid-ce"):
        embedder = BgeEmbedder(model_name=model_name)
    if mode == "hybrid-rerank":
        reranker = LexicalReranker([c.text for c in chunks])
    elif mode == "hybrid-ce":
        reranker = CrossEncoderReranker([c.text for c in chunks],
                                        model_name=reranker_model)
    return HybridSearcher(chunks, embedder=embedder, reranker=reranker,
                          k_fuse=k_fuse, k_final=k_final)
