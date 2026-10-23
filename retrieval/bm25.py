# -*- coding: utf-8 -*-
"""BM25（Okapi）。几十到几万 chunk 用纯 Python 足够，先别上 ES。"""
import math
from collections import Counter
from typing import Dict, List, Sequence, Tuple


class BM25:
    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [list(d) for d in docs]
        self.tf: List[Counter] = [Counter(d) for d in self.docs]
        self.dl = [len(d) for d in self.docs]
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf: Dict[str, float] = {
            t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def score(self, terms: Sequence[str], idx: int) -> float:
        if not self.docs:
            return 0.0
        tf, dl = self.tf[idx], self.dl[idx]
        s = 0.0
        for t in terms:
            f = tf.get(t)
            if not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
            s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
        return s

    def search(self, terms: Sequence[str], top_k: int = 50) -> List[Tuple[int, float]]:
        scored = [(i, self.score(terms, i)) for i in range(len(self.docs))]
        scored = [x for x in scored if x[1] > 0]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]
