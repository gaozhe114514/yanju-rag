# -*- coding: utf-8 -*-
"""考点卡片：加载、筛选、列表/详情序列化。

卡片是项目自写的考点摘要，不是教材原文；每张卡片对应一个检索 chunk，
正文写成可以直接逐字引用的完整句子，引用校验沿用真题同一套逻辑。
"""
import json
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import SUBJECT_ORDER, brief, subject_label

REQUIRED_FIELDS = ("id", "subject", "chapter", "title", "body")
SUBJECTS = ("ds", "co", "os", "cn")

# 章节按 408 大纲的常规顺序排，不依赖 JSONL 的行序
CHAPTER_ORDER = {
    "ds": ["算法分析", "线性表", "栈与队列", "串", "树与二叉树", "图", "查找", "排序", "字符串"],
    "co": ["计算机系统概述", "数据的表示", "存储系统", "指令系统", "中央处理器", "输入输出系统"],
    "os": ["操作系统概述", "进程与线程", "处理机调度", "内存管理", "同步与死锁", "文件系统", "I/O 管理"],
    "cn": ["体系结构", "物理层", "数据链路层", "网络层", "传输层", "应用层"],
}


def _chapter_rank(subject: str, chapter: str) -> int:
    order = CHAPTER_ORDER.get(subject or "", [])
    try:
        return order.index(chapter)
    except ValueError:
        return len(order)


def _subject_rank(subject: str) -> int:
    return SUBJECT_ORDER.index(subject) if subject in SUBJECT_ORDER else len(SUBJECT_ORDER)


def load_knowledge(path: str, display_path: str = "") -> List[Dict]:
    """读考点 JSONL。同一 id 后出现的覆盖先出现的。"""
    cards: Dict[str, Dict] = {}
    name = display_path or path
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("%s:%d 不是合法 JSON：%s" % (name, lineno, exc))
            missing = [k for k in REQUIRED_FIELDS if not str(card.get(k) or "").strip()]
            if missing:
                raise ValueError("%s:%d 缺少字段：%s" % (name, lineno, ", ".join(missing)))
            if card["subject"] not in SUBJECTS:
                raise ValueError("%s:%d 未知科目：%s" % (name, lineno, card["subject"]))
            card = dict(card)
            card["_file"] = name
            card["_line"] = lineno
            card["related_question_ids"] = [
                str(x) for x in (card.get("related_question_ids") or []) if str(x).strip()
            ]
            cards[card["id"]] = card
    return list(cards.values())


def haystack(card: Dict) -> str:
    parts = [
        str(card.get("id") or ""),
        str(card.get("title") or ""),
        str(card.get("chapter") or ""),
        subject_label(card.get("subject") or ""),
        str(card.get("summary") or ""),
        str(card.get("body") or ""),
        str(card.get("pitfalls") or ""),
        " ".join(str(x) for x in (card.get("formulas") or [])),
    ]
    return " ".join(parts).lower()


def know_brief(card: Dict) -> Dict:
    """列表项：不带正文，避免一次返回几十张卡片的大段文字。"""
    return {
        "id": card.get("id"),
        "subject": card.get("subject") or "",
        "subject_label": subject_label(card.get("subject") or ""),
        "chapter": card.get("chapter") or "",
        "title": card.get("title") or "",
        "summary": card.get("summary") or "",
        "status": card.get("status") or "draft",
        "source": card.get("source") or "self",
        "related_count": len(card.get("related_question_ids") or []),
    }


def know_detail(card: Dict, questions_by_id: Optional[Dict[str, Dict]] = None) -> Dict:
    item = know_brief(card)
    item.update({
        "body": card.get("body") or "",
        "formulas": list(card.get("formulas") or []),
        "pitfalls": card.get("pitfalls") or "",
        "related_question_ids": list(card.get("related_question_ids") or []),
        "source_ref": {
            "file": card.get("_file") or "",
            "line": card.get("_line") or 0,
            "page": None,
        },
        # 卡片是自写摘要，前端要把这个来源标注出来，不能伪装成教材原文
        "provenance": "项目自写考点摘要（非教材原文）",
    })
    by_id = questions_by_id or {}
    related = []
    missing = []
    for qid in item["related_question_ids"]:
        q = by_id.get(qid)
        if q is None:
            missing.append(qid)
        else:
            related.append(brief(q))
    item["related_questions"] = related
    item["missing_questions"] = missing
    return item


def filter_knowledge(cards: Sequence[Dict], query: str = "", subject: str = "",
                     chapter: str = "", limit: int = 20, offset: int = 0
                     ) -> Tuple[int, List[Dict]]:
    q = (query or "").strip().lower()
    sub = (subject or "").strip()
    chap = (chapter or "").strip()
    matched: List[Dict] = []
    for card in cards:
        if sub and (card.get("subject") or "") != sub:
            continue
        if chap and (card.get("chapter") or "") != chap:
            continue
        if q and q not in haystack(card):
            continue
        matched.append(card)
    matched.sort(key=lambda c: (_subject_rank(c.get("subject") or ""),
                                _chapter_rank(c.get("subject") or "",
                                              c.get("chapter") or ""),
                                c.get("title") or ""))
    total = len(matched)
    return total, matched[offset:offset + limit]


def knowledge_subject_counts(cards: Iterable[Dict]) -> List[Dict]:
    """四个科目都返回，数量为 0 也保留，前端导航才稳定。"""
    counts: Dict[str, int] = {}
    for card in cards:
        key = card.get("subject") or ""
        counts[key] = counts.get(key, 0) + 1
    return [{"key": key, "label": subject_label(key), "count": counts.get(key, 0)}
            for key in SUBJECT_ORDER if key]


def _year_of(question: Dict) -> Optional[int]:
    try:
        return int(question.get("year"))
    except (TypeError, ValueError):
        return None


def knowledge_chapters(cards: Sequence[Dict], subject: str = "",
                       questions_by_id: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    """章节计数。传入 questions_by_id 时一并算该章节关联的真题数（考频）。"""
    counts: Dict[str, int] = {}
    linked: Dict[str, set] = {}
    by_id = questions_by_id or {}
    for card in cards:
        if subject and (card.get("subject") or "") != subject:
            continue
        chap = card.get("chapter") or "未分章"
        counts[chap] = counts.get(chap, 0) + 1
        bucket = linked.setdefault(chap, set())
        for qid in card.get("related_question_ids") or []:
            # 只在题库里能对上的题才算考频，避免把脏 id 算进热度
            if not by_id or qid in by_id:
                bucket.add(qid)
    # 同一科目下按大纲章节顺序；未知章节排在最后
    order = CHAPTER_ORDER.get(subject or "", [])
    names = sorted(counts, key=lambda n: (order.index(n) if n in order else len(order), n))
    rows = [{"name": chap, "count": counts[chap]} for chap in names]
    if questions_by_id is not None:
        for row in rows:
            row["question_count"] = len(linked.get(row["name"], ()))
    return rows


def card_heat(cards: Sequence[Dict],
              questions_by_id: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    """每张考点卡片的考频：关联真题数、年份分布、对不上的题目数。"""
    by_id = questions_by_id or {}
    out: List[Dict] = []
    for card in cards:
        ids = list(card.get("related_question_ids") or [])
        years: Counter = Counter()
        missing = 0
        for qid in ids:
            question = by_id.get(qid)
            if by_id and question is None:
                missing += 1
                continue
            year = _year_of(question or {})
            if year is not None:
                years[year] += 1
        item = know_brief(card)
        item["related_question_ids"] = ids
        item["heat"] = len(ids)
        item["years"] = {str(y): n for y, n in sorted(years.items())}
        item["missing"] = missing
        out.append(item)
    # 考频高的在前；同频按科目、章节、标题排，保证结果稳定可复现
    out.sort(key=lambda c: (-c["heat"],
                            _subject_rank(c.get("subject") or ""),
                            _chapter_rank(c.get("subject") or "", c.get("chapter") or ""),
                            c.get("title") or ""))
    return out


def knowledge_tree(cards: Sequence[Dict],
                   questions_by_id: Optional[Dict[str, Dict]] = None,
                   limit: int = 20) -> Dict:
    """科目 → 章节的考点树，附章节考频和考频榜。

    考频的口径是「关联真题条数」，同一道题被两张卡片关联会各算一次；
    章节层的 question_count 去重，反映该章节实际覆盖多少道真题。
    """
    by_id = questions_by_id or {}
    heat = card_heat(cards, by_id)
    ranked = [c for c in heat if c["heat"] > 0]
    subjects: List[Dict] = []
    for key in SUBJECT_ORDER:
        if not key:
            continue
        rows = [c for c in cards if (c.get("subject") or "") == key]
        if not rows:
            continue
        chapters = knowledge_chapters(rows, key, by_id)
        covered = set()
        for card in rows:
            covered.update(qid for qid in (card.get("related_question_ids") or [])
                           if not by_id or qid in by_id)
        subjects.append({
            "key": key,
            "label": subject_label(key),
            "cards": len(rows),
            "questions": len(covered),
            "question_heat": sum(c["heat"] for c in heat if c.get("subject") == key),
            "chapters": chapters,
        })
    return {
        "subjects": subjects,
        "heat": ranked[:max(0, int(limit))],
        "total_cards": len(cards),
        "total_heat": sum(c["heat"] for c in heat),
        "linked_questions": len(set(
            qid for card in cards for qid in (card.get("related_question_ids") or [])
            if not by_id or qid in by_id)),
        "questions": len(by_id),
    }


def knowledge_for_question(cards: Sequence[Dict], question_id: str) -> List[Dict]:
    qid = str(question_id or "")
    return [know_brief(c) for c in cards if qid in (c.get("related_question_ids") or [])]


def related_knowledge_by_question(cards: Sequence[Dict]) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for card in cards:
        item = know_brief(card)
        for qid in card.get("related_question_ids") or []:
            out.setdefault(qid, []).append(item)
    return out
