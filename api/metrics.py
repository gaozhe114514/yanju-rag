# -*- coding: utf-8 -*-
"""进程内指标：本进程启动以来的问答计数。

只做内存计数，不落盘、不跨进程——单 worker 部署下够用。
真正的长期数据在 gap_logs 与 answer_cache 里，这里补的是「今天用了多少次」。
"""
import threading
from typing import Dict

_lock = threading.Lock()
_counters: Dict[str, int] = {}

# 允许的键，写错名字时能早点发现
KEYS = (
    "ask_total", "ask_answered", "ask_refused",
    "chat_total", "chat_answered", "chat_refused", "chat_nonkb", "chat_daily",
    # 资料里查不到依据、改走通用回答的次数
    "chat_fallback",
    "cache_hit",
    # 精通之路的导师会话（定大纲）：轮次、给了大纲、问了问题、模型没用
    "mastery_total", "mastery_outline", "mastery_ask", "mastery_failed",
    # Agent 端点：轮次、回答/拒答、工具调用次数、换查询重试次数
    "agent_total", "agent_answered", "agent_refused", "agent_tool_calls",
    "agent_retry",
)


def bump(name: str, n: int = 1) -> None:
    if name not in KEYS:
        raise KeyError("未登记的指标：%s" % name)
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def snapshot() -> Dict[str, int]:
    """补齐所有键，前端不用判断 undefined。"""
    with _lock:
        return {k: _counters.get(k, 0) for k in KEYS}


def reset() -> None:
    with _lock:
        _counters.clear()
