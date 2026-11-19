# -*- coding: utf-8 -*-
"""双路召回 → RRF → 精排。"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

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


# 「实时数据」问法跳过精排的门槛。本地 bge-small 实测（277 个 chunk）：
# 在库问题 BM25 最高分 ≥6.4、向量分 ≥0.51；「今天开封市天气」「红烧肉怎么做」
# 这类库外问题 BM25 ≤4.0、向量 ≤0.49。两个条件同时满足才跳过——
# 宁可多花几秒，也不误拒在库问题。
_SKIP_BM25 = 5.0
_SKIP_VEC = 0.55


def weak_evidence(bm_hits: Sequence[tuple], vmap: Dict[int, float]) -> bool:
    """双路召回都拿不出像样的证据：精排救不回来，也不用花那几秒。"""
    top_bm = max((s for _, s in bm_hits), default=0.0)
    if top_bm >= _SKIP_BM25:
        return False
    if not vmap:
        return True
    return max(vmap.values()) < _SKIP_VEC


class HybridSearcher:
    def __init__(self, chunks: Sequence[Chunk], embedder=None,
                 reranker=None, k_each: int = 50, k_fuse: int = 30,
                 k_final: int = 5, vector_search=None):
        """vector_search：外部向量召回（如 pgvector），签名 (vec, top_k) -> [(id, score)]。

        传入后不再把语料向量放进进程内存，也不在构造时重复算一遍；
        BM25 仍然在进程内，因为没有中文分词的 Postgres 全文检索帮不上忙。
        """
        if not chunks:
            raise ValueError("chunks 为空")
        self.chunks = list(chunks)
        self._index_by_id = {c.id: i for i, c in enumerate(self.chunks)}
        self.k_each, self.k_fuse, self.k_final = k_each, k_fuse, k_final
        self.bm25 = BM25([tokenize(c.text) for c in self.chunks])
        self.embedder = embedder
        self.reranker = reranker
        self.vector_search = vector_search
        self.vector_index: Optional[VectorIndex] = None
        if embedder is not None and vector_search is None:
            vecs = embedder.embed([c.text for c in self.chunks])
            self.vector_index = VectorIndex(vecs)

    def search(self, query: str, skip_rerank: bool = False) -> List[SearchHit]:
        bm_hits = self.bm25.search(tokenize(query), self.k_each)
        bmap = dict(bm_hits)
        lists = [bm_hits]
        vmap = {}
        if (self.vector_index is not None or self.vector_search is not None) \
                and self.embedder is not None:
            qv = self.embedder.embed([query], is_query=True)[0]
            if self.vector_search is not None:
                # 数据库返回的是 chunk id，转回本进程里的下标，后续统一走 RRF
                v_hits = [(self._index_by_id[cid], score)
                          for cid, score in self.vector_search(qv, self.k_each)
                          if cid in self._index_by_id]
            else:
                v_hits = self.vector_index.search(qv, self.k_each)
            vmap = dict(v_hits)
            lists.append(v_hits)

        if skip_rerank and self.reranker is not None and weak_evidence(bm_hits, vmap):
            # 实时数据类问题（今天几号、开封天气）：双路召回都没有像样的证据，
            # 资料里不可能有能逐字引用的原文。精排要 5~7 秒，救不回这类问题，
            # 直接返回空让闸门拒答，省掉这段时间。
            return []

        fused = rrf(lists, k=60, top_k=self.k_fuse)
        ranked = (self.reranker.rerank(query, fused, self.k_final)
                  if self.reranker is not None else fused[:self.k_final])
        return [SearchHit(self.chunks[i], s, bmap.get(i, 0.0), vmap.get(i, 0.0))
                for i, s in ranked]


MODES = ("bm25", "hybrid", "hybrid-rerank", "hybrid-ce")


def build_searcher(chunks: Sequence[Chunk], mode: str = "hybrid",
                   model_name: str = "BAAI/bge-small-zh-v1.5",
                   reranker_model: str = "BAAI/bge-reranker-v2-m3",
                   k_fuse: int = 30, k_final: int = 5,
                   embedder=None, vector_search=None,
                   reranker=None) -> HybridSearcher:
    if mode not in MODES:
        raise ValueError("未知 mode: %s" % mode)
    if mode in ("hybrid", "hybrid-rerank", "hybrid-ce") and embedder is None:
        embedder = BgeEmbedder(model_name=model_name)
    if reranker is None and mode == "hybrid-rerank":
        reranker = LexicalReranker([c.text for c in chunks])
    elif reranker is None and mode == "hybrid-ce":
        reranker = CrossEncoderReranker([c.text for c in chunks],
                                        model_name=reranker_model)
    return HybridSearcher(chunks, embedder=embedder, reranker=reranker,
                          k_fuse=k_fuse, k_final=k_final,
                          vector_search=vector_search)
