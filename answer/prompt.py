# -*- coding: utf-8 -*-
"""生成阶段的 prompt：把检索结果当唯一事实来源，强制 JSON 输出。"""
from typing import Dict, List, Sequence

from retrieval.search import SearchHit

SYSTEM_PROMPT = """你是 408 考研助教。必须遵守以下规则：
1. 只能使用【资料】中的内容回答，不得使用资料以外的知识。
2. 每一条结论都必须有引用；没有引用就不许下结论。
3. 引用必须逐字摘自资料原文，必须是连续的一段，长度不少于 10 个字；
   优先引用包含结论的完整句子，不要只引用选项字母（例如「答案：D」）、
   单个数字或半个公式；「[id] 类型=…」「答案：」「解析：」是资料标注，
   不是原文，quote 里不要带这些前缀；不要用省略号拼接不连续的文字。
   chunk_id 必须是资料中给出的 id。
4. 资料分两类：类型=真题 的题干、答案、解析，以及 类型=考点 的卡片正文。
   两类都可以引用；引用考点时只能抄卡片里写出的正文、公式或易错点。
   问某道选择题的答案时，引用该答案字母对应的选项整行（如「D. …」），
   或解析里能推出答案的句子；不要引用只有字母的答案行。
5. 考点卡片是项目自写的概念摘要，可用于解释概念和原理；但涉及具体题目答案时，
   以真题的答案和解析为准。两者冲突时明确说明并以真题为准。
6. 资料不足以回答时，refused 设为 true，并在 reason 中说明缺什么，严禁猜测。
7. 只输出一个 JSON 对象，不要 Markdown 代码块，不要额外解释。

JSON 格式：
{"refused": false, "answer": "结论", "citations": [{"chunk_id": "2019-408-31", "quote": "原文片段"}], "reason": ""}
"""


def format_hits(hits: Sequence[SearchHit]) -> str:
    blocks: List[str] = []
    for h in hits:
        c = h.chunk
        if c.kind == "knowledge":
            lines = ["[%s] 类型=考点 科目=%s 章节=%s 标题=%s"
                     % (c.id, c.subject or "?", c.chapter or "?", c.title or "?")]
            lines.append(c.text)
        else:
            lines = ["[%s] 类型=真题 科目=%s 题号=%d" % (c.id, c.subject or "?", c.seq)]
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
            "去掉空白后不少于 10 个字，优先抄完整的句子，不要用省略号拼接；"
            "quote 不要带「答案：」「解析：」等标注前缀，也不要只写一个选项字母；"
            "chunk_id 必须在资料里出现过。若无法给出合规引用，就把 refused 设为 true。"
        ) % feedback,
    })
    return msgs
