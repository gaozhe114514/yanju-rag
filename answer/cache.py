# -*- coding: utf-8 -*-
"""应答缓存：查询归一化、缓存键、TTL 与响应包装。

只缓存「答上来了」的响应；拒答不进缓存。缓存本体落在 PostgreSQL
的 answer_cache 表，本模块只做纯函数，便于单测。
"""
import hashlib
import os
import re
from typing import Dict

DEFAULT_TTL_S = 86400
_WS = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """折叠空白并统一大小写，让「CSMA/CD」和「csma/cd 」共用一条缓存。"""
    return _WS.sub(" ", (query or "").strip()).casefold()


def cache_key(query: str, namespace: str = "") -> str:
    """缓存键 = 命名空间 + 归一化问题；命名空间隔离模式/模型/语料版本。"""
    raw = "%s\n%s" % (namespace or "", normalize_query(query))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def namespace(base: str, style: str = "", collection: str = "",
              preset: bool = True) -> str:
    """在基础命名空间上叠加伙伴风格与知识库。

    内置语料沿用原命名空间（历史缓存还能命中）；自建库各自加一段，
    否则甲的答案会被喂给乙的资料。风格按原文指纹入键，换个伙伴重新生成。
    """
    ns = base or ""
    if not preset:
        ns += "\ncollection:" + (collection or "")
    if style:
        ns += "\nstyle:" + hashlib.sha1(style.encode("utf-8")).hexdigest()[:16]
    return ns


def ttl_from_env(name: str = "KB_CACHE_TTL", default: int = DEFAULT_TTL_S) -> int:
    """读取 TTL（秒）；非法值回退默认，0 或负数表示关闭缓存。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def is_fresh(age_s: float, ttl_s: int) -> bool:
    return ttl_s > 0 and 0 <= age_s <= ttl_s


def storable_payload(payload: Dict) -> Dict:
    """写入缓存前去掉每次请求都会变的字段。"""
    return {k: v for k, v in payload.items()
            if k not in ("cached", "cache_age_s", "latency_ms")}


def with_cache_meta(payload: Dict) -> Dict:
    """未命中缓存的正常响应：显式标出 cached=false。"""
    out = dict(payload)
    out["cached"] = False
    out["cache_age_s"] = 0
    return out


def cached_payload(payload: Dict, age_s: float, latency_ms: int) -> Dict:
    """命中缓存：保留答案与引用，标注年龄，执行过程换成缓存一步。"""
    out = dict(payload)
    age = max(0.0, float(age_s))
    out["cached"] = True
    out["cache_age_s"] = round(age, 1)
    out["latency_ms"] = int(latency_ms)
    out["trace"] = {
        "intent": out.get("intent", "kb"),
        "rewritten_query": out.get("rewritten_query", ""),
        "steps": [{
            "name": "应答缓存",
            "ms": int(latency_ms),
            "detail": "命中缓存：回答生成于 %d 秒前" % int(age),
        }],
    }
    return out
