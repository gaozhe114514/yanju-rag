# -*- coding: utf-8 -*-
"""工具并行预取：拆快慢、判断实时数据、取结果时的超时兜底。

这里不碰 FastAPI，单测可以直接导入（CI 不安装 fastapi）。
"""
from concurrent.futures import TimeoutError as FutureTimeout
from typing import List

from answer.tools import tool_will_run

# 工具自己每个外部调用最多 TIMEOUT_S 秒，天气要打两个接口；这里给足，
# 免得等到了超时才发现结果被扔掉。工具从检索那一刻就在跑，不从这行才开始计时。
TOOL_WAIT_S = 9.0
# 问的是「此刻」的事实：静态资料库里不可能有能逐字引用的原文
_LIVE_TOOLS = ("weather", "time", "calc")
# 可以抢跑的工具：本地算或轻量外部查询。搜索 / 抓网页不打，免得资料能答时白花外部额度
_PREFETCH_TOOLS = ("weather", "time", "calc", "study", "code")


def _split_tools(names) -> tuple:
    """拆成「可以和检索并行跑」与「必须等到真降级再跑」两组。"""
    fast, slow = [], []
    for name in names or []:
        name = str(name or "").strip()
        if not name:
            continue
        (fast if name in _PREFETCH_TOOLS else slow).append(name)
    return fast, slow


def _live_query(names, query: str) -> bool:
    """这轮是不是实时数据问题（天气 / 日期 / 算术）。"""
    for name in names or []:
        name = str(name or "").strip()
        if name in _LIVE_TOOLS and tool_will_run([name], query):
            return True
    return False


def _tool_results(future, timeout: float = TOOL_WAIT_S) -> List[dict]:
    """取并行工具的结果；超时就如实说没等到，不拿半截数据凑答案。"""
    if future is None:
        return []
    try:
        return future.result(timeout=timeout) or []
    except FutureTimeout:
        return [{"name": "tool", "label": "实时数据", "source": "",
                 "detail": "没在规定时间内返回，这次不用它", "ok": False}]
    except Exception as e:  # 工具自己炸了不能影响回答
        return [{"name": "tool", "label": "实时数据", "source": "",
                 "detail": "调用出错（%s）" % type(e).__name__, "ok": False}]
