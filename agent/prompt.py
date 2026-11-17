# -*- coding: utf-8 -*-
"""Agent 专用的两段提示：引用校验失败后的重试，以及换查询。

生成提示本身仍用 answer.prompt 的 build_messages / build_retry_messages，
不另起一套规则；这里只补「上一次没通过，换一个检索问题再来一次」。
"""
from typing import List, Sequence, Tuple

from answer.prompt import build_retry_messages as _build_retry
from answer.providers import parse_payload
from retrieval.search import SearchHit

REFINE_SYSTEM = """你是「言据」资料问答系统的检索规划器。
上一次回答没有通过逐字引用校验，需要换一个更容易在资料里找到原文的检索问题。

只输出 JSON：
{"query":"新的检索问题","reason":"一句话说明为什么换"}

规则：
1. 不改变用户原意，不加入资料外的事实、年份或题号；
2. 保留专业术语与中英文缩写，不超过 80 字；
3. 不要回答问题，只给检索问题；
4. 如果原问题已经足够具体，可以只调整措辞或补同义术语。
"""


def build_retry_messages(query: str, hits: Sequence[SearchHit], previous: str,
                         errors: Sequence[str], style: str = "",
                         mode: str = "exam", stats: str = "") -> List[dict]:
    """沿用主链路的引用纠错提示，保证重试仍受同一套硬约束。"""
    return _build_retry(query, hits, previous, errors, style, mode, stats)


def _rule_refine(query: str, hits: Sequence[SearchHit]) -> str:
    """没有模型时的兜底：把最相关片段的标题补进检索词。"""
    if not hits:
        return query
    c = hits[0].chunk
    hint = (c.title or c.chapter or "").strip()
    if not hint or hint in query:
        return query
    merged = (query + " " + hint).strip()
    return merged[:80]


def refine_query(provider, query: str, errors: Sequence[str],
                 hits: Sequence[SearchHit]) -> Tuple[str, str]:
    """返回 (新检索问题, 来源)。来源是 llm / rule，方便前端展示。"""
    q = (query or "").strip()
    brief = "\n".join("- " + str(e)[:160] for e in (errors or [])[:4])
    titles = []
    for h in (hits or [])[:3]:
        label = (h.chunk.title or h.chunk.chapter or h.chunk.id)
        titles.append("- %s" % label)
    context = "原检索问题：%s" % q
    if brief:
        context += "\n校验没通过的原因：\n%s" % brief
    if titles:
        context += "\n上次命中的资料：\n%s" % "\n".join(titles)
    if provider is not None and hasattr(provider, "complete"):
        try:
            raw = provider.complete(
                [{"role": "system", "content": REFINE_SYSTEM},
                 {"role": "user", "content": context}],
                temperature=0.0, max_tokens=120, json_mode=True)
            obj = parse_payload(raw)
            new_q = str(obj.get("query") or "").strip()[:80]
            if new_q:
                return new_q, "llm"
        except Exception:  # 改写失败不能挡住答题，退回规则
            pass
    return _rule_refine(q, hits), "rule"
