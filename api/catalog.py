# -*- coding: utf-8 -*-
"""题库浏览用的纯函数：科目标签、列表筛选、详情序列化。"""
from typing import Dict, Iterable, List, Sequence, Tuple

SUBJECT_LABELS = {
    "ds": "数据结构",
    "co": "计算机组成原理",
    "os": "操作系统",
    "cn": "计算机网络",
    "": "未分类",
}
SUBJECT_ORDER = ("ds", "co", "os", "cn", "")


def subject_label(key: str) -> str:
    return SUBJECT_LABELS.get(key or "", key or "未分类")


def _haystack(q: Dict) -> str:
    opts = q.get("options") or {}
    parts = [
        str(q.get("id") or ""),
        str(q.get("stem") or ""),
        subject_label(q.get("subject") or ""),
    ]
    parts.extend(str(opts.get(k) or "") for k in "ABCD")
    return " ".join(parts).lower()


def brief(q: Dict) -> Dict:
    stem = (q.get("stem") or "").strip()
    return {
        "id": q.get("id"),
        "seq": q.get("seq"),
        "year": q.get("year"),
        "type": q.get("type") or "single",
        "subject": q.get("subject") or "",
        "subject_label": subject_label(q.get("subject") or ""),
        "stem": stem,
        "options": q.get("options") or {},
        "has_options": bool(q.get("options")),
    }


def detail(q: Dict) -> Dict:
    item = brief(q)
    figures = q.get("figures") or []
    item.update({
        "score": q.get("score"),
        "options": q.get("options") or {},
        "answer": q.get("answer") or "",
        "analysis": q.get("analysis") or "",
        "analysis_origin": q.get("analysis_origin") or "",
        "status": q.get("status") or "draft",
        "source_ref": q.get("source_ref") or {},
        "figures": figures,
        # 题干提到图但数据里没有图，前端提示“需对照原卷”
        "needs_figure": ("图" in (q.get("stem") or "")) and not figures,
    })
    return item


def filter_questions(rows: Sequence[Dict], query: str = "", subject: str = "",
                     year=None, limit: int = 20, offset: int = 0
                     ) -> Tuple[int, List[Dict]]:
    q = (query or "").strip().lower()
    sub = (subject or "").strip()
    matched: List[Dict] = []
    for row in rows:
        row_sub = row.get("subject") or ""
        if sub == "none":
            if row_sub:
                continue
        elif sub and row_sub != sub:
            continue
        if year is not None and str(row.get("year") or "") != str(year):
            continue
        if q and q not in _haystack(row):
            continue
        matched.append(row)
    total = len(matched)
    return total, matched[offset:offset + limit]


def subject_counts(rows: Iterable[Dict]) -> List[Dict]:
    counts: Dict[str, int] = {}
    for row in rows:
        key = row.get("subject") or ""
        counts[key] = counts.get(key, 0) + 1
    out = []
    for key in SUBJECT_ORDER:
        if counts.get(key):
            out.append({"key": key, "label": subject_label(key), "count": counts[key]})
    for key, n in counts.items():
        if key not in SUBJECT_ORDER:
            out.append({"key": key, "label": subject_label(key), "count": n})
    return out
