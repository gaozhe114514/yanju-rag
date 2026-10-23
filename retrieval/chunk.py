# -*- coding: utf-8 -*-
"""题库 JSONL → 检索单元。

一道题 = 一个 chunk。但「用于召回的文本」只放题干和选项：
答案与解析一起参与 embedding 时，学生问概念很容易命中「某年某题 + 它的解析」，
候选集会被同类题占满。答案和解析照样随 chunk 返回，供生成和引用使用。
"""
import json
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional


@dataclass
class Chunk:
    id: str
    seq: int
    text: str
    answer: str = ""
    analysis: str = ""
    subject: str = ""
    kp_ids: List[str] = field(default_factory=list)
    source_ref: Dict = field(default_factory=dict)
    # 新增字段一律放在末尾：测试和外部代码用 Chunk(id, seq, text) 位置参数构造
    kind: str = "question"       # question / knowledge
    title: str = ""
    chapter: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


def build_chunk(q: Dict) -> Chunk:
    options = q.get("options") or {}
    parts = [(q.get("stem") or "").strip()]
    for k in "ABCD":
        v = (options.get(k) or "").strip()
        if v:
            parts.append("%s. %s" % (k, v))
    return Chunk(
        id=q["id"],
        seq=int(q.get("seq") or 0),
        text="\n".join(p for p in parts if p),
        answer=q.get("answer") or "",
        analysis=q.get("analysis") or "",
        subject=q.get("subject") or "",
        kp_ids=list(q.get("kp_ids") or []),
        source_ref=dict(q.get("source_ref") or {}),
    )


def build_knowledge_chunk(card: Dict, source_file: str = "", line: int = 0) -> Chunk:
    """一张考点卡片 = 一个 chunk。

    召回文本把标题、正文、公式、易错点都放进去；引用校验只认这里的原文，
    所以卡片正文必须写成可以直接引用的完整句子。
    """
    parts = [(card.get("title") or "").strip()]
    chapter = (card.get("chapter") or "").strip()
    if chapter:
        parts.append("章节：%s" % chapter)
    body = (card.get("body") or "").strip()
    if body:
        parts.append(body)
    formulas = [f.strip() for f in (card.get("formulas") or []) if f and f.strip()]
    if formulas:
        parts.append("关键公式：%s" % "；".join(formulas))
    pitfalls = (card.get("pitfalls") or "").strip()
    if pitfalls:
        parts.append("易错点：%s" % pitfalls)
    return Chunk(
        id=card["id"],
        seq=0,
        text="\n".join(p for p in parts if p),
        subject=card.get("subject") or "",
        source_ref={
            "file": source_file,
            "line": int(line),
            "page": None,
        },
        kind="knowledge",
        title=(card.get("title") or "").strip(),
        chapter=chapter,
    )


def _sort_key(qid: str):
    parts = qid.split("-")
    try:
        return (int(parts[0]), int(parts[-1]))
    except (ValueError, IndexError):
        return (0, 0)


def load_questions(paths: Iterable[str]) -> List[Dict]:
    """读多个 JSONL，按 id 去重（后面的覆盖前面的），按年份+题号排序。"""
    rows: Dict[str, Dict] = {}
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    q = json.loads(line)
                    rows[q["id"]] = q
    return [rows[k] for k in sorted(rows, key=_sort_key)]


def load_chunks(paths: Iterable[str]) -> List[Chunk]:
    return [build_chunk(q) for q in load_questions(paths)]
