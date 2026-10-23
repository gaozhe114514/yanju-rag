# -*- coding: utf-8 -*-
"""双路召回 → RRF → 精排。"""
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
        return [SearchHit(self.chunks[i], s, bmap.get(i, 0.0), vmap.get(i, 0.0))
                for i, s in ranked]


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
