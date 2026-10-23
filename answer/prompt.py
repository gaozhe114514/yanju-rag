# -*- coding: utf-8 -*-
"""生成阶段的 prompt：把检索结果当唯一事实来源，强制 JSON 输出。"""
from typing import Dict, List, Sequence

from retrieval.search import SearchHit

SYSTEM_PROMPT = """你是 408 考研助教。必须遵守以下规则：
1. 只能使用【资料】中的内容回答，不得使用资料以外的知识。
2. 每一条结论都必须有引用；没有引用就不许下结论。
3. 引用必须逐字摘自资料原文，chunk_id 必须是资料中给出的 id。
4. 资料不足以回答时，refused 设为 true，并在 reason 中说明缺什么，严禁猜测。
5. 只输出一个 JSON 对象，不要 Markdown 代码块，不要额外解释。

JSON 格式：
{"refused": false, "answer": "结论", "citations": [{"chunk_id": "2019-408-31", "quote": "原文片段"}], "reason": ""}
"""


def format_hits(hits: Sequence[SearchHit]) -> str:
    blocks: List[str] = []
    for h in hits:
        c = h.chunk
        lines = ["[%s] 科目=%s 题号=%d" % (c.id, c.subject or "?", c.seq)]
        lines.append(c.text)
        if c.answer:
            lines.append("答案：%s" % c.answer)
        if c.analysis:
            lines.append("解析：%s" % c.analysis)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_messages(query: str, hits: Sequence[SearchHit]) -> List[Dict]:
    user = "【资料】\n%s\n\n【问题】\n%s" % (format_hits(hits), query.strip())
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def build_retry_messages(query: str, hits: Sequence[SearchHit],
                         previous: str, errors: Sequence[str]) -> List[Dict]:
    msgs = build_messages(query, hits)
    msgs.append({"role": "assistant", "content": previous})
    feedback = "\n".join("- " + e for e in errors)
    msgs.append({
        "role": "user",
        "content": (
            "上面的输出没有通过引用校验：\n%s\n\n"
            "请重新输出 JSON。每一条引用都必须是【资料】中某个 chunk 的连续原文，"
            "chunk_id 必须在资料里出现过。若无法给出合规引用，就把 refused 设为 true。"
        ) % feedback,
    })
    return msgs
