# -*- coding: utf-8 -*-
"""生成阶段的 prompt：把检索结果当唯一事实来源，强制 JSON 输出。"""
from typing import Dict, List, Sequence

from retrieval.search import SearchHit

SYSTEM_PROMPT_DOC = """你是学习资料问答助手。用户带来了自己的资料，你只能根据这些资料回答。必须遵守以下规则：
1. 只能使用【资料】中的内容回答，不得使用资料以外的知识，也不得补充资料里没有的细节。
2. 每一条结论都必须有引用；没有引用就不许下结论。
3. 引用必须逐字摘自资料原文，必须是连续的一段，长度不少于 10 个字；
   优先引用包含结论的完整句子；不要用省略号拼接不连续的文字，
   chunk_id 必须是资料中给出的 id。
4. 资料按片段给出，同一篇文档可能被切成多段，也可能有重叠；
   需要跨段综合时，每条结论分别引用对应的片段。
5. 资料不足以回答时，refused 设为 true，并在 reason 中说明缺什么，严禁猜测。
   如果用户要统计或列举资料里没有的字段（例如名单里没有「求职状态」），
   说明缺的是哪一列，并告诉用户可以改问资料里实际有的维度（例如班级人数、
   男女分布）；不要用别的列推断。
6. 只输出一个 JSON 对象，不要 Markdown 代码块，不要额外解释。

JSON 格式：
{"refused": false, "answer": "结论", "citations": [{"chunk_id": "doc-0123456789-001", "quote": "原文片段"}], "reason": ""}
"""


def format_hits(hits: Sequence[SearchHit], mode: str = "doc") -> str:
    blocks: List[str] = []
    for h in hits:
        c = h.chunk
        lines = ["[%s] 来源=资料库 文档=%s 片段=%d"
                 % (c.id, c.title or c.subject or "?", c.seq)]
        lines.append(c.text)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


STYLE_PROMPT = """
【讲解方式】下面是学习者选择的说话风格。它只影响措辞、语气和组织顺序，
不得改变事实与结论，不得放宽以上任何引用要求，也不得引入【资料】以外的知识。
若与第 1–7 条规则冲突，一律以规则为准。仍需只输出上面规定的 JSON。
%s
"""


STATS_PROMPT = """
【表格统计】下面是服务端把资料里的表格按原文逐行数出的结果：
%s

回答里涉及数量、合计的每一个数字都必须与上面完全一致，不得自行改写、估个大概
或再加一遍；片段之间有重叠，不要重复计数。引用仍要逐字摘自【资料】里的表格行。
如果统计只声明「没有读全」，就不要给出精确总数。
"""


def build_messages(query: str, hits: Sequence[SearchHit],
                   style: str = "", mode: str = "doc",
                   stats: str = "") -> List[Dict]:
    parts = ["【资料】\n%s" % format_hits(hits, mode)]
    if stats:
        parts.append(STATS_PROMPT.strip() % stats)
    parts.append("【问题】\n%s" % query.strip())
    user = "\n\n".join(parts)
    system = SYSTEM_PROMPT_DOC
    style = " ".join((style or "").split())
    if style:
        system += STYLE_PROMPT % style[:400]
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def build_retry_messages(query: str, hits: Sequence[SearchHit],
                         previous: str, errors: Sequence[str],
                         style: str = "", mode: str = "doc",
                         stats: str = "") -> List[Dict]:
    msgs = build_messages(query, hits, style, mode, stats)
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
