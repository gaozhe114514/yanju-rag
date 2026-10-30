# -*- coding: utf-8 -*-
"""日常对话：不检索、不引用，纯粹和模型聊天办事。

资料模式（默认）里每条结论都必须逐字对上检索到的原文，对不上就拒答；
这条链路太窄，做不了「帮我改个句子」「排个计划」这类日常请求。
所以日常对话单独一条链路：不碰检索库、不产出引用，也不允许自称
「根据资料」。两条链路用请求里的 mode 区分，引用校验等硬规则只约束资料模式。
"""
from typing import Dict, Iterator, List, Optional

from .agent import normalize_history

# 离线演示（ExtractiveProvider / 没有模型）时的回答：说实话，不假装生成
OFFLINE_REPLY = ("现在是离线演示模式，没有接生成模型，日常对话答不了。"
                 "切到「资料」模式仍可按资料原文回答。")

DAILY_SYSTEM = """你是「引证」里的日常助手，和用户正常聊天、帮点小忙。

规则：
1. 直接回答，用日常口气。要步骤、清单、草稿就写清楚，别绕。
2. 这个模式不查资料，所以不要说「根据资料」「原文说」，也不要编造出处、页码、题号。
3. 如果问题其实是在问某份资料里的内容（真题、考点、讲义或笔记），就直说：这条建议切到
   「资料」模式再问，那边会给可逐字核对的原文引用。不要凭记忆作答。
4. 不知道就说不知道，不编。
5. 默认用中文；用户换语言就跟着换。
"""


def build_daily_messages(query: str, history: Optional[List[Dict]] = None,
                         style: str = "", tool_text: str = "") -> List[Dict]:
    """拼日常对话的 messages。历史只留 user/assistant 的短文本。"""
    messages: List[Dict] = [{"role": "system", "content": DAILY_SYSTEM}]
    if style:
        messages.append({"role": "system", "content": "讲解方式：" + style[:400]})
    # 工具结果放在历史之前：它是这一轮的实时事实，不是聊天记录
    if tool_text:
        messages.append({"role": "system", "content": tool_text[:2000]})
    messages.extend(normalize_history(history, 6))
    messages.append({"role": "user", "content": (query or "").strip()})
    return messages


def daily_reply(provider, query: str, history: Optional[List[Dict]] = None,
                style: str = "", tool_text: str = "") -> str:
    """非流式日常回答。没有可用模型时返回离线说明，不假装答过。"""
    if provider is None or not hasattr(provider, "complete"):
        return OFFLINE_REPLY
    text = provider.complete(build_daily_messages(query, history, style, tool_text),
                             temperature=0.6, max_tokens=700, json_mode=False)
    return (text or "").strip() or "（模型这次没给出内容，再说一遍试试。）"


def daily_stream(provider, query: str, history: Optional[List[Dict]] = None,
                 style: str = "", tool_text: str = "") -> Iterator[str]:
    """流式日常回答：模型支持流式就走流式，否则退成一整段。"""
    if provider is not None and hasattr(provider, "chat_stream"):
        yield from provider.chat_stream(
            build_daily_messages(query, history, style, tool_text),
            temperature=0.6, max_tokens=700)
        return
    yield daily_reply(provider, query, history, style, tool_text)
