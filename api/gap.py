# -*- coding: utf-8 -*-
"""拒答缺口日志：记录没答上来的问题，作为补资料/改检索的输入。"""
import json
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

_lock = threading.Lock()


def log_gap(path: str, query: str, reason: str, top_hit=None) -> dict:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": query,
        "reason": reason,
        "top_chunk_id": getattr(getattr(top_hit, "chunk", None), "id", ""),
        "top_score": round(float(getattr(top_hit, "score", 0.0)), 4),
    }
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def reason_bucket(reason: str) -> str:
    """把 "citation_failed: 引用过短" 归到 citation_failed，便于聚合。"""
    text = (reason or "").strip()
    if not text:
        return "unknown"
    return text.split(":", 1)[0].strip() or "unknown"


# 这些是系统判定码；之外的拒答原因才是模型说的「资料里缺什么」
_GAP_CODES = {"no_hits", "low_score", "low_margin", "citation_failed",
              "provider_error", "empty_collection", "unknown", "server_not_ready"}


def gap_detail(reason: str, limit: int = 160) -> str:
    """取出模型自己说的缺口，给前端展示；系统判定码和空话一律返回空串。"""
    text = " ".join(str(reason or "").split())
    if not text or text == "模型判断资料不足":
        return ""
    if text.split(":", 1)[0].strip() in _GAP_CODES:
        return ""
    return text[:limit]


def _parse_ts(value) -> Optional[datetime]:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def gap_stats(path: str, days: int = 30, limit: int = 20) -> Dict:
    """JSONL 模式下的缺口统计，字段与 db.store.Store.gap_stats 对齐。"""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reasons = Counter()
    by_day = Counter()
    recent = []
    total = 0

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
                reasons[reason_bucket(rec.get("reason"))] += 1
                by_day[ts.astimezone(timezone.utc).strftime("%Y-%m-%d")] += 1
                recent.append({
                    "ts": ts.isoformat(timespec="seconds"),
                    "query": rec.get("query") or "",
                    "reason": rec.get("reason") or "",
                    "top_chunk": rec.get("top_chunk_id") or None,
                    "top_score": rec.get("top_score"),
                })

    recent.sort(key=lambda r: r["ts"], reverse=True)
    return {
        "storage": "jsonl",
        "window_days": days,
        "total": total,
        "by_reason": [{"reason": r, "count": n}
                      for r, n in reasons.most_common()],
        "by_day": [{"date": d, "count": n} for d, n in sorted(by_day.items())],
        "recent": recent[:limit],
    }
