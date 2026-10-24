# -*- coding: utf-8 -*-
"""回答反馈：把「有没有用」变成可统计、可追查的记录。

和 gap_logs 的区别：gap 记的是「没答上来」，feedback 记的是「答了但用户
不满意」。两者一起看，才知道该补资料还是该改检索/生成。

没有数据库时落 JSONL，字段与 db.store.Store.feedback_stats 对齐。
"""
import json
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

_lock = threading.Lock()

# 前端只传 key，文案在服务端统一，避免同一原因出现多种写法
REASONS = {
    "wrong_answer": "答非所问",
    "bad_citation": "引用不对",
    "should_answer": "资料里有但没答",
    "other": "其他",
}

QUERY_MAX = 500
ANSWER_MAX = 2000
NOTE_MAX = 500
CITATIONS_MAX = 50


def reason_label(key: str) -> str:
    return REASONS.get((key or "").strip(), REASONS["other"])


def normalize_feedback(query: str, helpful: bool, reason: str = "",
                       note: str = "", answer: str = "",
                       citations: Optional[Sequence[str]] = None,
                       cached: bool = False) -> Dict:
    """收敛成入库形状；非法输入抛 ValueError，由接口转成 422。"""
    q = " ".join(str(query or "").split())[:QUERY_MAX]
    if not q:
        raise ValueError("query 不能为空")
    key = "" if helpful else ((reason or "").strip() or "other")
    if key and key not in REASONS:
        key = "other"
    ids: List[str] = []
    for cid in citations or []:
        text = str(cid or "").strip()[:80]
        if text and text not in ids:
            ids.append(text)
        if len(ids) >= CITATIONS_MAX:
            break
    return {
        "query": q,
        "answer": str(answer or "").strip()[:ANSWER_MAX],
        "helpful": bool(helpful),
        "reason": key,
        "note": str(note or "").strip()[:NOTE_MAX],
        "citations": ids,
        "cached": bool(cached),
    }


def log_feedback(path: str, rec: Dict) -> Dict:
    row = dict(rec)
    row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _parse_ts(value) -> Optional[datetime]:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def feedback_stats(path: str, days: int = 30, limit: int = 10) -> Dict:
    """JSONL 模式下的反馈统计；只把差评放进 recent，正反馈看比例即可。"""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reasons = Counter()
    total = helpful = 0
    recent = []

    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        fh = None
    if fh is not None:
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # 坏行不能拖垮统计
                ts = _parse_ts(rec.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                total += 1
                ok = bool(rec.get("helpful"))
                if ok:
                    helpful += 1
                    continue
                reasons[rec.get("reason") or "other"] += 1
                recent.append({
                    "ts": ts.isoformat(timespec="seconds"),
                    "query": rec.get("query") or "",
                    "reason": rec.get("reason") or "other",
                    "note": rec.get("note") or "",
                    "answer": rec.get("answer") or "",
                    "citations": rec.get("citations") or [],
                    "cached": bool(rec.get("cached")),
                })

    recent.sort(key=lambda r: r["ts"], reverse=True)
    return {
        "storage": "jsonl",
        "window_days": days,
        "total": total,
        "helpful": helpful,
        "not_helpful": total - helpful,
        "helpful_rate": round(helpful / total, 4) if total else 0.0,
        "by_reason": [{"reason": r, "count": n}
                      for r, n in reasons.most_common()],
        "recent": recent[:limit],
    }
