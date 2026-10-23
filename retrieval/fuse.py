# -*- coding: utf-8 -*-
"""Reciprocal Rank Fusion：按名次合并，绕开 BM25 与余弦的量纲差异。"""
from typing import Dict, List, Sequence, Tuple


def rrf(results: Sequence[Sequence[Tuple[int, float]]], k: int = 60,
        top_k: int = 30) -> List[Tuple[int, float]]:
    scores: Dict[int, float] = {}
    for lst in results:
        for rank, (idx, _) in enumerate(lst, 1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:top_k]
