# -*- coding: utf-8 -*-
"""检索 → 拒答 → 生成 → 引用校验（失败重试一次）→ 响应。"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from retrieval.search import HybridSearcher, SearchHit

from .aggregate import (MAX_SCAN_CHARS, PARTIAL_NOTE, TableStats,
                        check_counts, needs_full_doc, table_stats)
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
    # 实际交给模型 / 用于校验的上下文；读全表或补相邻片段时会多于 hits。
    # hits 仍是检索结果，界面上的「召回 N 条」按它显示。
    context: List[SearchHit] = field(default_factory=list)
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
        """按问题类型准备交给模型的上下文。

        - 数量 / 名单 / 平均极值 / 全文总结：必须读全同一库的片段，否则答案
          会缺行或缺段，而且看起来仍然像模像样。库太大（超过一次能通读的
          上限）就退回 top-k，并明确告诉模型「没读全」，不许报精确数字。
        - 普通问答：只给检索结果，再补上同文档紧挨着的前后片段，避免一段话
          被切在两块里、答案只读到一半。
        """
        if self.prompt_mode != "doc":
            return hits, None
        chunks = getattr(self.searcher, "chunks", None)
        if not chunks:
            return hits, None
        if needs_full_doc(query):
            total = sum(len(c.text) for c in chunks)
            if total > MAX_SCAN_CHARS:
                return hits, TableStats(
                    text=PARTIAL_NOTE % (total, MAX_SCAN_CHARS), partial=True)
            seen = {h.chunk.id for h in hits}
            context = list(hits) + [SearchHit(c, 0.0) for c in chunks
                                    if c.id not in seen]
            # 只数「召回命中过的那几份文档」：同一库里别的表格如果也有「男 25 行」，
            # 把它的数字也放进允许值，会让对不上的答案蒙混过关。
            def doc_key(chunk):
                ref = chunk.source_ref or {}
                return ref.get("document_id") or chunk.title or chunk.id

            hit_docs = {doc_key(h.chunk) for h in hits}
            focus = [h.chunk for h in context if doc_key(h.chunk) in hit_docs]
            focus = focus or [h.chunk for h in context]
            # 进料时抽好的结构化表格跟着检索器走；没有就退回从正文数
            return context, table_stats(
                focus, query, tables=getattr(self.searcher, "doc_tables", None))
        return self._with_neighbors(hits, chunks), None

    def prepare(self, query: str, hits):
        """给 Agent 层复用的上下文准备（相邻片段 / 全表统计），行为与主链路一致。"""
        return self._prepare(query, hits)

    # 普通问答补前后片段的上限：top-k 之外不再无限加
    NEIGHBOR_MAX_CHARS = 12000

    def _with_neighbors(self, hits, chunks):
        """给每条命中补同一文档里紧挨着的前后片段（WeKnora 的邻居扩展开）。

        检索命中的往往是一段话或表格的中间几行，前后被切开；补上邻居能让
        模型看到完整句群。只补一层、只补同文档，并有总字数上限。
        """
        def doc_key(chunk):
            ref = chunk.source_ref or {}
            return ref.get("document_id") or chunk.title or chunk.id

        by_doc = {}
        for c in chunks:
            by_doc.setdefault(doc_key(c), []).append(c)
        for seq in by_doc.values():
            seq.sort(key=lambda c: (c.source_ref or {}).get("start", c.seq))

        chosen = list(hits)
        seen = {h.chunk.id for h in hits}
        used = sum(len(h.chunk.text) for h in hits)
        for h in hits:
            seq = by_doc.get(doc_key(h.chunk)) or []
            index = {c.id: i for i, c in enumerate(seq)}
            i = index.get(h.chunk.id)
            if i is None:
                continue
            for j in (i - 1, i + 1):
                if not (0 <= j < len(seq)):
                    continue
                c = seq[j]
                if c.id in seen:
                    continue
                if used + len(c.text) > self.NEIGHBOR_MAX_CHARS:
                    continue
                chosen.append(SearchHit(c, 0.0))
                seen.add(c.id)
                used += len(c.text)
        return chosen

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

        context, stats = self._prepare(query, hits)
        stats_text = stats.text if stats else ""
        messages = build_messages(query, context, style, self.prompt_mode,
                                  stats_text)
        errors: List[str] = []
        first_errors: List[str] = []
        stat_errors: List[str] = []
        raw = ""
        for attempt in (1, 2):
            stat_errors = []
            tg = time.time()
            try:
                raw = self.provider.generate(messages, hits=context)
            except ProviderError as e:
                spent["generate"] += int((time.time() - tg) * 1000)
                return done(AnswerResult(True, reason="provider_error: %s" % e,
                                         hits=hits, context=context,
                                         attempts=attempt,
                                         errors=first_errors))
            spent["generate"] += int((time.time() - tg) * 1000)

            tv = time.time()
            try:
                payload = parse_payload(raw)
            except ProviderError as e:
                errors = [str(e)]
            else:
                errors = verify_payload(payload, context)
                if not errors and not payload["refused"]:
                    errors = check_counts(str(payload.get("answer") or ""), stats)
                    stat_errors = list(errors)
                if not errors:
                    spent["verify"] += int((time.time() - tv) * 1000)
                    if payload["refused"]:
                        return done(AnswerResult(
                            True, reason=str(payload.get("reason") or "模型判断资料不足"),
                            hits=hits, context=context, attempts=attempt,
                            errors=first_errors))
                    return done(AnswerResult(
                        False,
                        answer=str(payload.get("answer") or ""),
                        citations=list(payload.get("citations") or []),
                        hits=hits, context=context, attempts=attempt,
                        errors=first_errors))
            spent["verify"] += int((time.time() - tv) * 1000)

            if attempt == 1 and self.retry:
                first_errors = list(errors)
                messages = build_retry_messages(query, context, raw, errors, style,
                                                self.prompt_mode, stats_text)
                continue
            reason = "stat_failed: " if stat_errors else "citation_failed: "
            return done(AnswerResult(True, reason=reason + "; ".join(errors),
                                     hits=hits, context=context, attempts=attempt,
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

        context, stats = self._prepare(query, hits)
        stats_text = stats.text if stats else ""
        messages = build_messages(query, context, style, self.prompt_mode,
                                  stats_text)
        first_errors: List[str] = []
        stat_errors: List[str] = []
        for attempt in (1, 2):
            stat_errors = []
            yield {"type": "status", "stage": "generate", "attempt": attempt}
            started = time.time()
            buf: List[str] = []
            extractor = AnswerStream()
            try:
                for chunk in generate_chunks(self.provider, messages, context):
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
                           context=context, attempts=attempt,
                           errors=first_errors))}
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
                errors = verify_payload(payload, context)
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
                                   hits=hits, context=context, attempts=attempt,
                                   errors=first_errors))}
                        return
                    yield {"type": "done",
                           "result": done(AnswerResult(
                               False,
                               answer=str(payload.get("answer") or ""),
                               citations=list(payload.get("citations") or []),
                               hits=hits, context=context, attempts=attempt,
                               errors=first_errors))}
                    return
            timings["verify"] = int((time.time() - started) * 1000)

            if attempt == 1 and self.retry:
                first_errors = list(errors)
                yield {"type": "reset",
                       "note": ("统计数字和表格对不上，正在重新生成" if stat_errors
                                else "引用没有逐字对上原文，正在重新生成")}
                messages = build_retry_messages(query, context, raw, errors, style,
                                                self.prompt_mode, stats_text)
                continue
            reason = "stat_failed: " if stat_errors else "citation_failed: "
            yield {"type": "done",
                   "result": done(AnswerResult(
                       True, reason=reason + "; ".join(errors),
                       hits=hits, context=context, attempts=attempt,
                       errors=first_errors or list(errors)))}
            return

        yield {"type": "done",
               "result": done(AnswerResult(True, reason="unknown", hits=hits, attempts=2))}
