# -*- coding: utf-8 -*-
"""检索 → 拒答 → 生成 → 引用校验（失败重试一次）→ 响应。"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from retrieval.search import HybridSearcher, SearchHit

from .aggregate import (MAX_SCAN_CHARS, PARTIAL_NOTE, TableStats,
                        check_counts, is_count_question, table_stats)
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
                 gate: Optional[RefusalGate] = None, retry: bool = True,
                 prompt_mode: str = "exam"):
        self.searcher = searcher
        self.provider = provider
        self.gate = gate or RefusalGate()
        self.retry = retry
        # exam：408 真题 + 考点；doc：用户自建资料库
        self.prompt_mode = prompt_mode

    def _prepare(self, query: str, hits):
        """统计类问题：读全同库片段，并让服务端把表格数出来。

        只在用户自建资料库生效。库太大（超过一次能通读的上限）就退回 top-k，
        同时告诉模型「没读全」，不许它报精确总数——宁可说不知道，也不给错数字。
        """
        if self.prompt_mode != "doc" or not is_count_question(query):
            return hits, None
        chunks = getattr(self.searcher, "chunks", None)
        if not chunks:
            return hits, None
        total = sum(len(c.text) for c in chunks)
        if total > MAX_SCAN_CHARS:
            return hits, TableStats(
                text=PARTIAL_NOTE % (total, MAX_SCAN_CHARS), partial=True)
        seen = {h.chunk.id for h in hits}
        expanded = list(hits) + [SearchHit(c, 0.0) for c in chunks
                                 if c.id not in seen]
        # 只数「召回命中过的那几份文档」：同一库里别的表格如果也有「男 25 行」，
        # 把它的数字也放进允许值，会让对不上的答案蒙混过关。
        def doc_key(chunk):
            ref = chunk.source_ref or {}
            return ref.get("document_id") or chunk.title or chunk.id

        hit_docs = {doc_key(h.chunk) for h in hits}
        focus = [h.chunk for h in expanded if doc_key(h.chunk) in hit_docs]
        focus = focus or [h.chunk for h in expanded]
        return expanded, table_stats(focus, query)

    def answer(self, query: str, style: str = "",
               skip_rerank: bool = False) -> AnswerResult:
        t0 = time.time()
        hits = self.searcher.search(query, skip_rerank=skip_rerank)
        t1 = time.time()
        decision = self.gate.check(query, hits)
        t2 = time.time()
        timings = {
            "retrieve": int((t1 - t0) * 1000),
            "gate": int((t2 - t1) * 1000),
        }
        if skip_rerank and not hits and getattr(self.searcher, "reranker", None):
            timings["rerank_skipped"] = 1
        spent = {"generate": 0, "verify": 0}

        def done(res: AnswerResult) -> AnswerResult:
            res.timings = {**timings, **spent}
            return res

        if decision.refuse:
            return done(AnswerResult(True, reason=decision.reason, hits=hits))

        hits, stats = self._prepare(query, hits)
        stats_text = stats.text if stats else ""
        messages = build_messages(query, hits, style, self.prompt_mode, stats_text)
        errors: List[str] = []
        first_errors: List[str] = []
        stat_errors: List[str] = []
        raw = ""
        for attempt in (1, 2):
            stat_errors = []
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
                if not errors and not payload["refused"]:
                    errors = check_counts(str(payload.get("answer") or ""), stats)
                    stat_errors = list(errors)
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
                messages = build_retry_messages(query, hits, raw, errors, style,
                                                self.prompt_mode, stats_text)
                continue
            reason = "stat_failed: " if stat_errors else "citation_failed: "
            return done(AnswerResult(True, reason=reason + "; ".join(errors),
                                     hits=hits, attempts=attempt,
                                     errors=first_errors or list(errors)))

        return done(AnswerResult(True, reason="unknown", hits=hits, attempts=2))

    def answer_stream(self, query: str, style: str = "",
                      skip_rerank: bool = False):
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
        hits = self.searcher.search(query, skip_rerank=skip_rerank)
        t1 = time.time()
        decision = self.gate.check(query, hits)
        t2 = time.time()
        timings = {
            "retrieve": int((t1 - t0) * 1000),
            "gate": int((t2 - t1) * 1000),
        }
        if skip_rerank and not hits and getattr(self.searcher, "reranker", None):
            timings["rerank_skipped"] = 1
        yield {"type": "status", "stage": "gate", "hits": len(hits),
               "refuse": bool(decision.refuse)}

        def done(res: AnswerResult) -> AnswerResult:
            res.timings = timings
            return res

        if decision.refuse:
            yield {"type": "done",
                   "result": done(AnswerResult(True, reason=decision.reason, hits=hits))}
            return

        hits, stats = self._prepare(query, hits)
        stats_text = stats.text if stats else ""
        messages = build_messages(query, hits, style, self.prompt_mode, stats_text)
        first_errors: List[str] = []
        stat_errors: List[str] = []
        for attempt in (1, 2):
            stat_errors = []
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
                if not errors and not payload["refused"]:
                    errors = check_counts(str(payload.get("answer") or ""), stats)
                    stat_errors = list(errors)
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
                       "note": ("统计数字和表格对不上，正在重新生成" if stat_errors
                                else "引用没有逐字对上原文，正在重新生成")}
                messages = build_retry_messages(query, hits, raw, errors, style,
                                                self.prompt_mode, stats_text)
                continue
            reason = "stat_failed: " if stat_errors else "citation_failed: "
            yield {"type": "done",
                   "result": done(AnswerResult(
                       True, reason=reason + "; ".join(errors),
                       hits=hits, attempts=attempt,
                       errors=first_errors or list(errors)))}
            return

        yield {"type": "done",
               "result": done(AnswerResult(True, reason="unknown", hits=hits, attempts=2))}
