# -*- coding: utf-8 -*-
"""精排。

生产目标是 bge-reranker-v2-m3（Cross-Encoder）。开发期先用词面重叠加权，
把链路跑通；它不是 Cross-Encoder，别拿它的分数当线上阈值。
"""
from typing import List, Sequence, Tuple

from .tokenize import tokenize
from .vector import load_pretrained


class LexicalReranker:
    def __init__(self, texts: Sequence[str]):
        self.texts = list(texts)
        self.tokens = [set(tokenize(t)) for t in self.texts]

    def rerank(self, query: str, candidates: List[Tuple[int, float]],
               top_k: int = 5) -> List[Tuple[int, float]]:
        q = set(tokenize(query))
        scored = []
        for idx, base in candidates:
            d = self.tokens[idx]
            if not q or not d:
                overlap = 0.0
            else:
                inter = len(q & d)
                precision = inter / len(q)
                recall = inter / len(d)
                overlap = 0.0 if inter == 0 else 2 * precision * recall / (precision + recall)
            phrase = 0.1 if query.strip() and query.strip() in self.texts[idx] else 0.0
            # RRF 融合分只有 0.016~0.033，词面 F1 是 0~1；放大 base 才能让
            # 精排只做微调，而不是把双路召回的排序整个覆盖掉。
            scored.append((idx, 10.0 * base + overlap + phrase))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]


class CrossEncoderReranker:
    """bge-reranker 封装。首次使用时才加载模型，未下载时不要构造。"""

    def __init__(self, texts: Sequence[str], model_name: str = "BAAI/bge-reranker-v2-m3",
                 max_length: int = 512, device: str = ""):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        self.texts = list(texts)
        self.tokenizer = load_pretrained(AutoTokenizer.from_pretrained, model_name)
        self.model = load_pretrained(
            AutoModelForSequenceClassification.from_pretrained, model_name)
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device).eval()
        self.max_length = max_length

    def rerank(self, query: str, candidates: List[Tuple[int, float]],
               top_k: int = 5) -> List[Tuple[int, float]]:
        torch = self.torch
        pairs = [(query, self.texts[i]) for i, _ in candidates]
        batch = self.tokenizer(
            pairs, padding=True, truncation=True, max_length=self.max_length,
            return_tensors="pt",
        ).to(self.device)
        with torch.no_grad():
            logits = self.model(**batch).logits.view(-1)
        scores = torch.sigmoid(logits).cpu().tolist()
        scored = [(candidates[i][0], float(s)) for i, s in enumerate(scores)]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_k]
