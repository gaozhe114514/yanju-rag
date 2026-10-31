# -*- coding: utf-8 -*-
"""知识图谱：把考点卡与真题按「明确写出的关联」连成一张可以拖拽的图。

图的数据只有一个来源——考点卡 JSONL 里的 ``related_question_ids``。
不按科目、章节或相似度自动补边：图上出现的每一条线都能指回某张卡片的
某一项，和问答里「引用必须逐字命中原文」是同一套口径。

节点（都对应一个 chunk）：
- knowledge：考点卡，一个考点一个 chunk；
- question：真题，一道题一个 chunk。

过滤规则：
- subject 同时作用于两类节点；
- year 只作用于真题，筛选后没有剩下一道题的考点卡一并隐藏，
  否则整屏都是没有连线的卡片；
- 没被任何考点卡引用的真题默认不显示（和 Obsidian 默认隐藏孤立笔记一样），
  include_orphans=True 时一起画出来，方便发现「有题没配考点」的缺口。
"""
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import subject_label

KNOWLEDGE = "knowledge"
QUESTION = "question"


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _clip(text: str, limit: int = 80) -> str:
    text = _text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _knowledge_node(card: Dict) -> Dict:
    subject = card.get("subject") or ""
    title = _text(card.get("title")) or str(card.get("id") or "")
    return {
        "id": "k:" + str(card.get("id") or ""),
        "ref": str(card.get("id") or ""),
        "kind": KNOWLEDGE,
        "label": title,
        "title": title,
        "subject": subject,
        "subject_label": subject_label(subject),
        "chapter": _text(card.get("chapter")),
        "year": None,
        "seq": None,
        "summary": _clip(card.get("summary"), 90),
        "stem": "",
    }


def _question_node(q: Dict) -> Dict:
    subject = q.get("subject") or ""
    qid = str(q.get("id") or "")
    year = q.get("year")
    seq = q.get("seq")
    if year and isinstance(seq, int):
        label = "%s-%02d" % (year, seq)
        title = "%s 年第 %d 题" % (year, seq)
    else:
        label = qid
        title = qid
    return {
        "id": "q:" + qid,
        "ref": qid,
        "kind": QUESTION,
        "label": label,
        "title": title,
        "subject": subject,
        "subject_label": subject_label(subject),
        "chapter": "",
        "year": int(year) if str(year or "").strip().isdigit() else None,
        "seq": seq if isinstance(seq, int) else None,
        "summary": "",
        "stem": _clip(q.get("stem"), 90),
    }


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_graph(questions: Sequence[Dict], knowledge: Sequence[Dict],
                subject: str = "", year: Optional[int] = None,
                include_orphans: bool = False) -> Dict:
    """生成图谱数据。纯函数，不碰数据库，方便单测。"""
    sub = (subject or "").strip()
    year_key = str(year).strip() if year is not None and str(year).strip() else ""

    questions_by_id: Dict[str, Dict] = {}
    for q in questions:
        qid = str(q.get("id") or "")
        if not qid:
            continue
        if sub and (q.get("subject") or "") != sub:
            continue
        questions_by_id[qid] = q

    cards = []
    cards_by_id: Dict[str, Dict] = {}
    for card in knowledge:
        cid = str(card.get("id") or "")
        if not cid:
            continue
        if sub and (card.get("subject") or "") != sub:
            continue
        cards.append(card)
        cards_by_id[cid] = card

    # 卡片 → 题目：只保留语料里真实存在的题；指向未收录题目的关联单独记下来
    card_links: Dict[str, List[str]] = {}
    missing: List[Dict] = []
    for card in cards:
        cid = str(card.get("id"))
        targets = []
        for qid in _dedupe(str(x) for x in (card.get("related_question_ids") or [])):
            if qid in questions_by_id:
                targets.append(qid)
            else:
                missing.append({"card": cid, "question": qid})
        card_links[cid] = targets

    # 被卡片明确引用的题；其余都是「有题没配考点」的孤立题
    referenced = set()
    for targets in card_links.values():
        referenced.update(targets)

    def _year_of(qid):
        node = _question_node(questions_by_id[qid])
        return node["year"]

    # 年份下拉框按「当前科目下真正能画出来的题」来列，避免出现空年份
    year_source = set(questions_by_id) if include_orphans else referenced
    years = sorted({y for y in (_year_of(qid) for qid in year_source) if y})

    # 年份筛选：只筛题；卡片留不留看它还有没有剩下的题
    kept_questions = set(referenced)
    if year_key:
        kept_questions = {
            qid for qid in kept_questions if str(_year_of(qid) or "") == year_key
        }
    hidden_orphans = 0
    if not include_orphans:
        for qid in questions_by_id:
            if qid in referenced:
                continue
            if year_key and str(_year_of(qid) or "") != year_key:
                continue
            hidden_orphans += 1
    if include_orphans:
        # 开关打开时把没被引用的题也放进图里，它们会和卡片分开漂着
        kept_questions.update(
            qid for qid in questions_by_id
            if not year_key or str(_year_of(qid) or "") == year_key
        )

    kept_links: List[Tuple[str, str]] = []
    used_cards = set()
    for card in cards:
        cid = str(card.get("id"))
        for qid in card_links[cid]:
            if qid in kept_questions:
                kept_links.append((cid, qid))
                used_cards.add(cid)

    # 没有关联题的真题，默认不画；开关打开时补进来
    degree: Dict[str, int] = {}
    for cid, qid in kept_links:
        degree["k:" + cid] = degree.get("k:" + cid, 0) + 1
        degree["q:" + qid] = degree.get("q:" + qid, 0) + 1

    nodes: List[Dict] = []
    for card in cards:
        cid = str(card.get("id"))
        if year_key and cid not in used_cards:
            continue
        node = _knowledge_node(card)
        node["degree"] = degree.get(node["id"], 0)
        node["orphan"] = False
        nodes.append(node)

    shown_questions = set()
    for cid, qid in kept_links:
        shown_questions.add(qid)
    for qid in sorted(kept_questions):
        shown_questions.add(qid)
    for qid in sorted(shown_questions):
        node = _question_node(questions_by_id[qid])
        node["degree"] = degree.get(node["id"], 0)
        node["orphan"] = node["degree"] == 0
        nodes.append(node)

    links = [{"source": "k:" + cid, "target": "q:" + qid}
             for cid, qid in kept_links]

    subject_counts: Dict[str, Dict] = {}
    for node in nodes:
        key = node["subject"]
        row = subject_counts.setdefault(
            key, {"key": key, "label": node["subject_label"],
                  "knowledge": 0, "questions": 0})
        if node["kind"] == KNOWLEDGE:
            row["knowledge"] += 1
        else:
            row["questions"] += 1

    return {
        "nodes": nodes,
        "links": links,
        "missing": missing,
        "stats": {
            "knowledge": sum(1 for n in nodes if n["kind"] == KNOWLEDGE),
            "questions": sum(1 for n in nodes if n["kind"] == QUESTION),
            "links": len(links),
            "orphans": sum(1 for n in nodes if n["kind"] == QUESTION and n["orphan"]),
            "hidden_orphans": hidden_orphans,
            "missing": len(missing),
            "subjects": list(subject_counts.values()),
            "years": years,
            "total_knowledge": len(cards),
            "total_questions": len(questions_by_id),
        },
    }
