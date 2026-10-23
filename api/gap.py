# -*- coding: utf-8 -*-
"""拒答缺口日志：记录没答上来的问题，作为补资料/改检索的输入。"""
import json
import threading
from datetime import datetime, timezone
from typing import Optional

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
