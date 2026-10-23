# -*- coding: utf-8 -*-
"""检索 → 拒答 → 生成 → 引用校验（失败重试一次）→ 响应。"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from retrieval.search import HybridSearcher, SearchHit

from .gate import RefusalGate
from .prompt import build_messages, build_retry_messages
from .providers import ProviderError, parse_payload
from .verify import verify_payload


@dataclass
class AnswerResult:
    refused: bool
    answer: str = ""
    citations: List[Dict] = field(default_factory=list)
    reason: str = ""
    hits: List[SearchHit] = field(default_factory=list)
    attempts: int = 0


class AnswerPipeline:
    def __init__(self, searcher: HybridSearcher, provider,
                 gate: Optional[RefusalGate] = None, retry: bool = True):
        self.searcher = searcher
        self.provider = provider
        self.gate = gate or RefusalGate()
        self.retry = retry

    def answer(self, query: str) -> AnswerResult:
        hits = self.searcher.search(query)
        decision = self.gate.check(query, hits)
        if decision.refuse:
            return AnswerResult(True, reason=decision.reason, hits=hits)

        messages = build_messages(query, hits)
        errors: List[str] = []
        raw = ""
        for attempt in (1, 2):
            try:
                raw = self.provider.generate(messages, hits=hits)
            except ProviderError as e:
                return AnswerResult(True, reason="provider_error: %s" % e,
                                    hits=hits, attempts=attempt)
            try:
                payload = parse_payload(raw)
            except ProviderError as e:
                errors = [str(e)]
            else:
                if payload.get("refused"):
                    return AnswerResult(
                        True, reason=str(payload.get("reason") or "模型判断资料不足"),
                        hits=hits, attempts=attempt)
                errors = verify_payload(payload, hits)
                if not errors:
                    return AnswerResult(
                        False,
                        answer=str(payload.get("answer") or ""),
                        citations=list(payload.get("citations") or []),
                        hits=hits, attempts=attempt)

            if attempt == 1 and self.retry:
                messages = build_retry_messages(query, hits, raw, errors)
                continue
            return AnswerResult(True, reason="citation_failed: " + "; ".join(errors),
                                hits=hits, attempts=attempt)

        return AnswerResult(True, reason="unknown", hits=hits, attempts=2)
