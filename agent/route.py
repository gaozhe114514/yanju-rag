# -*- coding: utf-8 -*-
"""Agent 路由：决定这一轮是普通对话、查知识库，还是调用工具。

优先让模型判断，失败时退回规则。规则刻意保守：实时工具先认；明确提到
资料 / 文档 / 知识库的走知识库；情绪、寒暄、生活类问题走普通对话。
"""
import json
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from answer.providers import ProviderError, parse_payload
from answer.tools import tool_will_run

KB_CUES = (
    "资料", "文档", "知识库", "笔记", "讲义", "手册",
    "制度", "流程", "规范", "报销", "请假", "合同", "报价", "项目",
    "部署", "接口", "功能", "使用说明", "我的库",
    # 「公司」「产品」单独出现太宽：问外部公司是什么类型不该查自己的库，
    # 只有这些指向内部资料的说法才算知识库问题。
    "我们公司", "公司资料", "公司制度", "公司流程", "公司产品",
    "公司内部", "公司规定", "内部资料", "公司文档",
)
CHAT_CUES = (
    "我", "心情", "累", "不想", "难过", "开心", "烦", "焦虑", "建议",
    "怎么办", "聊聊", "你觉得", "陪我", "上班", "生活", "今天过得",
)
ROUTE_SYSTEM = """你是「言据」的回答路由器，只输出 JSON：
{"route":"chat"|"kb"|"tools","tools":["weather"],"reason":"一句话"}

- chat：寒暄、情绪、生活、行业常识，以及「某家外部公司 / 某款产品是什么」这类
  不需要用户自己资料的通用问题；
- kb：问题明确指向用户自己的文档、公司内部制度 / 流程 / 产品手册或知识库；
- tools：需要实时数据或外部网页，tools 从给定的可用工具里选；
- 不要回答问题，只做路由；reason 不超过 40 字。
"""


@dataclass
class RouteDecision:
    route: str                  # chat / kb / tools
    tools: List[str]
    reason: str = ""
    source: str = "rule"        # rule / llm


def _rule_route(query: str, collection: str, tools: Sequence[str]) -> RouteDecision:
    fired = [t for t in tools if tool_will_run([t], query)]
    if fired:
        return RouteDecision("tools", fired, "问法命中实时 / 网页工具")
    q = query or ""
    kb_score = sum(1 for cue in KB_CUES if cue in q)
    chat_score = sum(1 for cue in CHAT_CUES if cue in q)
    if kb_score > chat_score:
        return RouteDecision("kb", [], "问题指向公司资料或知识库")
    if chat_score > kb_score:
        return RouteDecision("chat", [], "情绪 / 生活 / 通用对话")
    if collection:
        return RouteDecision("kb", [], "已选资料范围，按资料问题处理")
    return RouteDecision("chat", [], "没有资料线索，按普通对话回答")


def route_query(query: str, history: Optional[Sequence[dict]], collection: str,
                provider=None, tools: Optional[Sequence[str]] = None) -> RouteDecision:
    enabled = [str(t) for t in (tools or []) if str(t).strip()]
    fired = [t for t in enabled if tool_will_run([t], query)]
    if fired:
        return RouteDecision("tools", fired, "问法命中实时 / 网页工具")

    if provider is not None and hasattr(provider, "complete"):
        try:
            lines = []
            for m in list(history or [])[-4:]:
                who = "用户" if m.get("role") == "user" else "助手"
                lines.append("%s：%s" % (who, str(m.get("content") or "")[:200]))
            user = ("可用工具：%s\n已选资料范围：%s\n对话历史：\n%s\n\n用户问题：%s"
                    % (", ".join(enabled) or "无", collection or "默认",
                       "\n".join(lines) or "无", query))
            raw = provider.complete(
                [{"role": "system", "content": ROUTE_SYSTEM},
                 {"role": "user", "content": user}],
                temperature=0.0, max_tokens=120, json_mode=True)
            obj = parse_payload(raw)
            route = str(obj.get("route") or "").strip()
            picked = [str(t) for t in (obj.get("tools") or [])
                      if str(t) in enabled and tool_will_run([str(t)], query)]
            if route == "tools" and picked:
                return RouteDecision("tools", picked,
                                     str(obj.get("reason") or "")[:80], "llm")
            if route == "kb" and not collection and not any(
                    cue in (query or "") for cue in KB_CUES):
                # 模型容易把「泛微网络是什么公司」当成公司资料问题；
                # 没选库、也没有内部资料线索时，按普通对话回答。
                return RouteDecision("chat", [], "通用问题，不需要查资料", "rule")
            if route in ("chat", "kb"):
                return RouteDecision(route, [],
                                     str(obj.get("reason") or "")[:80], "llm")
        except (ProviderError, ValueError, TypeError, KeyError):
            pass
    return _rule_route(query, collection, enabled)
