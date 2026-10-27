# -*- coding: utf-8 -*-
"""检索 → 拒答 → 生成 → 引用校验（失败重试一次）→ 响应。"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from retrieval.search import HybridSearcher, SearchHit

from .gate import RefusalGate
from .prompt import build_messages, build_retry_messages
from .providers import ProviderError, parse_payload
from .stream import AnswerStream, generate_chunks
from .verify import verify_payload


@dataclass
class AnswerResult:
    refused: bool
    answer: str = ""
    citations: List[Dict] = field(default_factory=list)
    reason: str = ""
    hits: List[SearchHit] = field(default_factory=list)
    attempts: int = 0
    errors: List[str] = field(default_factory=list)
    timings: Dict[str, int] = field(default_factory=dict)


class AnswerPipeline:
    def __init__(self, searcher: HybridSearcher, provider,
                 gate: Optional[RefusalGate] = None, retry: bool = True):
        self.searcher = searcher
        self.provider = provider
        self.gate = gate or RefusalGate()
        self.retry = retry

    def answer(self, query: str, style: str = "") -> AnswerResult:
        t0 = time.time()
        hits = self.searcher.search(query)
        t1 = time.time()
        decision = self.gate.check(query, hits)
        t2 = time.time()
        timings = {
            "retrieve": int((t1 - t0) * 1000),
            "gate": int((t2 - t1) * 1000),
        }
        spent = {"generate": 0, "verify": 0}

        def done(res: AnswerResult) -> AnswerResult:
            res.timings = {**timings, **spent}
            return res

        if decision.refuse:
            return done(AnswerResult(True, reason=decision.reason, hits=hits))

        messages = build_messages(query, hits, style)
        errors: List[str] = []
        first_errors: List[str] = []
        raw = ""
        for attempt in (1, 2):
            tg = time.time()
            try:
                raw = self.provider.generate(messages, hits=hits)
            except ProviderError as e:
                spent["generate"] += int((time.time() - tg) * 1000)
                return done(AnswerResult(True, reason="provider_error: %s" % e,
                                         hits=hits, attempts=attempt,
                                         errors=first_errors))
            spent["generate"] += int((time.time() - tg) * 1000)

            tv = time.time()
            try:
                payload = parse_payload(raw)
            except ProviderError as e:
                errors = [str(e)]
            else:
                errors = verify_payload(payload, hits)
                if not errors:
                    spent["verify"] += int((time.time() - tv) * 1000)
                    if payload["refused"]:
                        return done(AnswerResult(
                            True, reason=str(payload.get("reason") or "模型判断资料不足"),
                            hits=hits, attempts=attempt, errors=first_errors))
                    return done(AnswerResult(
                        False,
                        answer=str(payload.get("answer") or ""),
                        citations=list(payload.get("citations") or []),
                        hits=hits, attempts=attempt, errors=first_errors))
            spent["verify"] += int((time.time() - tv) * 1000)

            if attempt == 1 and self.retry:
                first_errors = list(errors)
                messages = build_retry_messages(query, hits, raw, errors, style)
                continue
            return done(AnswerResult(True, reason="citation_failed: " + "; ".join(errors),
                                     hits=hits, attempts=attempt,
                                     errors=first_errors or list(errors)))

        return done(AnswerResult(True, reason="unknown", hits=hits, attempts=2))

    def answer_stream(self, query: str, style: str = ""):
        """流式版本：先吐出 answer 文本，收完后照常校验引用。

        事件类型：
          status  阶段变化（retrieve / generate / verify / retry）
          delta   新增的 answer 文本
          reset   校验没过，之前显示的内容作废，前端要清空
          error   生成服务异常
          done    最终结果（AnswerResult），引用与拒答以它为准
        """
        t0 = time.time()
        yield {"type": "status", "stage": "retrieve"}
        hits = self.searcher.search(query)
        t1 = time.time()
        decision = self.gate.check(query, hits)
        t2 = time.time()
        timings = {
            "retrieve": int((t1 - t0) * 1000),
            "gate": int((t2 - t1) * 1000),
        }
        yield {"type": "status", "stage": "gate", "hits": len(hits),
               "refuse": bool(decision.refuse)}

        def done(res: AnswerResult) -> AnswerResult:
            res.timings = timings
            return res

        if decision.refuse:
            yield {"type": "done",
                   "result": done(AnswerResult(True, reason=decision.reason, hits=hits))}
            return

        messages = build_messages(query, hits, style)
        first_errors: List[str] = []
        for attempt in (1, 2):
            yield {"type": "status", "stage": "generate", "attempt": attempt}
            started = time.time()
            buf: List[str] = []
            extractor = AnswerStream()
            try:
                for chunk in generate_chunks(self.provider, messages, hits):
                    buf.append(chunk)
                    delta = extractor.feed(chunk)
                    if delta:
                        yield {"type": "delta", "text": delta}
            except ProviderError as e:
                timings["generate"] = int((time.time() - started) * 1000)
                yield {"type": "error", "message": str(e)}
                yield {"type": "done",
                       "result": done(AnswerResult(
                           True, reason="provider_error: %s" % e, hits=hits,
                           attempts=attempt, errors=first_errors))}
                return
            timings["generate"] = int((time.time() - started) * 1000)

            raw = "".join(buf)
            yield {"type": "status", "stage": "verify"}
            started = time.time()
            try:
                payload = parse_payload(raw)
            except ProviderError as e:
                errors = [str(e)]
            else:
                errors = verify_payload(payload, hits)
                if not errors:
                    timings["verify"] = int((time.time() - started) * 1000)
                    if payload["refused"]:
                        yield {"type": "done",
                               "result": done(AnswerResult(
                                   True,
                                   reason=str(payload.get("reason") or "模型判断资料不足"),
                                   hits=hits, attempts=attempt,
                                   errors=first_errors))}
                        return
                    yield {"type": "done",
                           "result": done(AnswerResult(
                               False,
                               answer=str(payload.get("answer") or ""),
                               citations=list(payload.get("citations") or []),
                               hits=hits, attempts=attempt,
                               errors=first_errors))}
                    return
            timings["verify"] = int((time.time() - started) * 1000)

            if attempt == 1 and self.retry:
                first_errors = list(errors)
                yield {"type": "reset",
                       "note": "引用没有逐字对上原文，正在重新生成"}
                messages = build_retry_messages(query, hits, raw, errors, style)
                continue
            yield {"type": "done",
                   "result": done(AnswerResult(
                       True, reason="citation_failed: " + "; ".join(errors),
                       hits=hits, attempts=attempt,
                       errors=first_errors or list(errors)))}
            return

        yield {"type": "done",
               "result": done(AnswerResult(True, reason="unknown", hits=hits, attempts=2))}
