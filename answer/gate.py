# -*- coding: utf-8 -*-
"""拒答闸门。

阈值不能拍脑袋：要用「该答的评测集」和「资料外的负样本」扫一遍，
取拒答负样本高、误拒正样本低的点。这里的默认值只是占位。

注意分数含义随检索模式变化：
- hybrid-ce：Cross-Encoder 的 sigmoid 概率，绝对阈值有意义；
- hybrid / bm25：RRF 分数只有 0.016~0.033，别拿 0.3 去卡。
"""
from dataclasses import dataclass
from typing import Sequence

from retrieval.search import SearchHit


@dataclass
class RefusalDecision:
    refuse: bool
    reason: str = ""
    top_score: float = 0.0
    margin: float = 0.0


class RefusalGate:
    def __init__(self, min_score: float = 0.0, min_margin: float = 0.0):
        self.min_score = min_score
        self.min_margin = min_margin

    def check(self, query: str, hits: Sequence[SearchHit]) -> RefusalDecision:
        if not hits:
            return RefusalDecision(True, "no_hits")
        top = float(hits[0].score)
        second = float(hits[1].score) if len(hits) > 1 else 0.0
        margin = top - second
        if top < self.min_score:
            return RefusalDecision(True, "low_score", top, margin)
        if margin < self.min_margin:
            return RefusalDecision(True, "ambiguous", top, margin)
        return RefusalDecision(False, "", top, margin)
