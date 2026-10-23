# -*- coding: utf-8 -*-
"""引用校验：生成结果里的 quote 必须逐字出现在被引 chunk 中。"""
import re
from typing import Dict, List, Sequence

from retrieval.search import SearchHit

MIN_QUOTE_CHARS = 6


def _norm(text: str) -> str:
    """去掉空白再比对。

    PDF 双栏抽取会在词中间插换行（如「后\n根遍历」），直接比字符串会误杀；
    标点和文字本身必须完全一致。
    """
    return re.sub(r"\s+", "", text or "")


def chunk_corpus(hit: SearchHit) -> str:
    c = hit.chunk
    return "\n".join(x for x in (c.text, c.answer, c.analysis) if x)


def verify_payload(payload: Dict, hits: Sequence[SearchHit],
                   min_quote_chars: int = MIN_QUOTE_CHARS) -> List[str]:
    """返回错误列表；空列表表示通过。

    refused=true 的结果不要求引用，由上层决定是否接受。
    """
    if not isinstance(payload, dict):
        return ["输出不是 JSON 对象"]
    if not isinstance(payload.get("refused"), bool):
        return ["refused 缺失或不是布尔值"]
    if payload["refused"]:
        return []

    errors: List[str] = []
    if not str(payload.get("answer") or "").strip():
        errors.append("answer 为空但 refused=false")

    citations = payload.get("citations")
    if not isinstance(citations, list) or not citations:
        return ["缺少 citations，结论没有引用"]

    by_id = {h.chunk.id: h for h in hits}
    for i, c in enumerate(citations, 1):
        if not isinstance(c, dict):
            errors.append("第 %d 条引用不是对象" % i)
            continue
        cid = str(c.get("chunk_id") or "")
        quote = str(c.get("quote") or "")
        hit = by_id.get(cid)
        if hit is None:
            errors.append("第 %d 条引用的 chunk_id=%s 不在候选集中" % (i, cid))
        elif len(_norm(quote)) < min_quote_chars:
            errors.append("第 %d 条引用过短，无法核实" % i)
        elif _norm(quote) not in _norm(chunk_corpus(hit)):
            errors.append("第 %d 条引用不是 %s 的原文片段" % (i, cid))
    return errors


def citations_ok(payload: Dict, hits: Sequence[SearchHit]) -> bool:
    return not verify_payload(payload, hits)
