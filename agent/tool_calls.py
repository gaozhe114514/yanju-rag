# -*- coding: utf-8 -*-
"""标准函数调用工具：模型自己选工具，不再用关键词路由。

工具分两类：
- search_kb / kb_overview：知识库检索与概览，由 Agent 节点交给原检索/校验链路；
- 其余工具：直接执行，把结果作为 tool 消息回给模型再回答。

工具描述就是模型看到的唯一路由依据；问法不命中关键词也能选对。
"""
from typing import Any, Dict, List, Sequence

from answer import tools as builtin

SEARCH_KB = "search_kb"
KB_OVERVIEW = "kb_overview"

# 前端设置页里的工具名 → 给模型看的说明
EXTERNAL_TOOLS = {
    "weather": {
        "name": "get_weather",
        "label": "天气查询",
        "description": "查询城市当前的天气和今天 / 明天的预报；问天气、气温、下不下雨时使用。",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名，例如上海"}},
            "required": ["city"],
        },
    },
    "time": {
        "name": "get_current_time",
        "label": "时间日期",
        "description": "取当前日期、星期和北京时间；问今天几号、现在几点时使用。",
        "parameters": {"type": "object", "properties": {}},
    },
    "calc": {
        "name": "calculate",
        "label": "计算换算",
        "description": "四则运算、百分比、平方 / 开方与常见单位换算。",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "要计算的表达式"}},
            "required": ["expression"],
        },
    },
    "code": {
        "name": "run_code",
        "label": "代码执行",
        "description": "进制转换、位运算和白名单内的短代码表达式。",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "要执行的表达式"}},
            "required": ["expression"],
        },
    },
    "search": {
        "name": "search_web",
        "label": "网页搜索",
        "description": "搜索公开网页上的最新信息；问新闻、行情、外部公司或产品的最新情况时使用。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索词"}},
            "required": ["query"],
        },
    },
    "fetch": {
        "name": "fetch_url",
        "label": "网页正文",
        "description": "抓取一个公开 http/https 网页的正文；用户给出链接，或已知官网地址时使用。",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "完整网址"}},
            "required": ["url"],
        },
    },
}

SEARCH_KB_SPEC = {
    "type": "function",
    "function": {
        "name": SEARCH_KB,
        "description": ("只在用户自己的知识库里检索可引用原文：公司内部制度 / 流程、"
                        "产品手册、用户上传的文档。问某家外部公司或产品是什么、"
                        "属于什么行业时不要用本工具，直接用已有知识回答。"),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索问题"}},
            "required": ["query"],
        },
    },
}

KB_OVERVIEW_SPEC = {
    "type": "function",
    "function": {
        "name": KB_OVERVIEW,
        "description": ("查看当前知识库的概览：库里有多少篇资料、分别叫什么。"
                        "问「这个库有多少资料 / 有哪些文档」时使用。"),
        "parameters": {"type": "object", "properties": {}},
    },
}


def tool_specs(enabled: Sequence[str]) -> List[Dict[str, Any]]:
    """当前开启的外部工具 + 永远可用的检索 / 概览工具。"""
    specs = [SEARCH_KB_SPEC, KB_OVERVIEW_SPEC]
    for key in enabled or []:
        item = EXTERNAL_TOOLS.get(str(key))
        if not item:
            continue
        specs.append({
            "type": "function",
            "function": {
                "name": item["name"],
                "description": item["description"],
                "parameters": item["parameters"],
            },
        })
    return specs


def _legacy_name(tool_name: str) -> str:
    for key, item in EXTERNAL_TOOLS.items():
        if item["name"] == tool_name:
            return key
    return ""


def execute(tool_name: str, args: Dict[str, Any], overview=None) -> Dict[str, Any]:
    """执行一个非检索工具，返回可直接放进 tool 消息的结果。"""
    if tool_name == KB_OVERVIEW:
        try:
            data = overview() if overview else {}
        except Exception as e:
            return {"name": KB_OVERVIEW, "label": "知识库概览", "ok": False,
                    "detail": "读取知识库概览失败：%s" % type(e).__name__}
        titles = list((data or {}).get("titles") or [])
        detail = "知识库「%s」：%d 篇资料，%d 段可引用片段。" % (
            (data or {}).get("name") or "当前知识库",
            int((data or {}).get("documents") or 0),
            int((data or {}).get("chunks") or 0))
        if titles:
            detail += "\n资料标题：" + "、".join(titles[:30])
        return {"name": KB_OVERVIEW, "label": "知识库概览", "source": "知识库",
                "ok": True, "detail": detail}

    key = _legacy_name(tool_name)
    if not key:
        return {"name": tool_name, "label": tool_name, "ok": False,
                "detail": "没有这个工具。"}
    if key == "weather":
        query = str((args or {}).get("city") or (args or {}).get("query") or "")
    elif key in ("calc", "code"):
        query = str((args or {}).get("expression") or (args or {}).get("query") or "")
    elif key == "fetch":
        query = str((args or {}).get("url") or (args or {}).get("query") or "")
    elif key == "search":
        query = str((args or {}).get("query") or "")
    else:
        query = str((args or {}).get("query") or "")
    handler = builtin._HANDLERS.get(key)
    if handler is None:
        return {"name": tool_name, "label": tool_name, "ok": False,
                "detail": "工具没有注册。"}
    try:
        result = handler(query)
    except Exception as e:
        return {"name": tool_name, "label": key, "ok": False,
                "detail": "工具执行失败：%s" % type(e).__name__}
    result = dict(result or {})
    result.setdefault("name", tool_name)
    return result


def openai_message_content(result: Dict[str, Any]) -> str:
    """把工具结果压成模型能读的一段文本。"""
    label = result.get("label") or result.get("name") or "工具"
    if not result.get("ok"):
        return "%s 失败：%s" % (label, result.get("detail") or "没有结果")
    return "%s（来源：%s）：\n%s" % (
        label, result.get("source") or "工具", result.get("detail") or "")
