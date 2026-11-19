# -*- coding: utf-8 -*-
"""不查资料的两条回答链路。

- daily：用户主动进入的日常对话（旧版 mode=chat，接口仍兼容）。
- general：自动问答里「资料查不到依据」的兜底；正文开头由服务端强制加上
  「资料里没有直接依据」，所以它不会被误当成资料原文，也不产出引用。

两条链路都不允许自称「根据资料」。引用必须逐字命中、资料没覆盖要明说，
这两条硬规则仍由检索与引用校验负责，这里只负责说人话。
"""
import re
from typing import Dict, Iterator, List, Optional

from .agent import normalize_history

# 离线演示（ExtractiveProvider / 没有模型）时的回答：说实话，不假装生成
OFFLINE_REPLY = ("现在是离线演示模式，没有接生成模型，这个问题答不了。"
                 "接入模型后可以直接问；资料里有的内容仍可按原文回答。")

# 服务端强制加在通用回答前面的一句话：不靠模型自觉，保证每次都明说
GENERAL_PREFIX = "资料里没有直接依据，下面按通用知识回答，不带引用。\n\n"

# 模型有时会把这句话抄进回答开头，服务端再拼一次就成了两遍。
# 开头是这几个说法的整行，一律当回声去掉。
_ECHO_HEADS = ("资料里没有直接依据", "资料中没有直接依据", "资料未覆盖")


def strip_echo(text: str) -> str:
    """去掉模型复述的那句「资料里没有直接依据」。"""
    out = (text or "").lstrip()
    if not any(out.startswith(h) for h in _ECHO_HEADS):
        return out
    nl = out.find("\n")
    return "" if nl < 0 else out[nl + 1:].lstrip("\n")


def _strip_echo_stream(chunks: Iterator[str]) -> Iterator[str]:
    """流式版：开头可能是回声时先攒着，确定不是回声就立刻往下传。"""
    buf = ""
    passthrough = False
    for chunk in chunks:
        if passthrough:
            yield chunk
            continue
        buf += chunk or ""
        head = buf.lstrip()
        if not head:
            continue
        if any(h.startswith(head) for h in _ECHO_HEADS):
            continue  # 还只收到开头几个字，等它长出来再判断
        if any(head.startswith(h) for h in _ECHO_HEADS):
            nl = head.find("\n")
            if nl < 0:
                if len(head) > 300:  # 没有换行的长回声：直接丢掉
                    passthrough = True
                    buf = ""
                continue
            head = head[nl + 1:].lstrip("\n")
            passthrough = True
            if head:
                yield head
            buf = ""
            continue
        passthrough = True
        yield head
        buf = ""
    if buf:
        rest = strip_echo(buf)
        if rest:
            yield rest

DAILY_SYSTEM = """你是「言据」里的助手，和用户正常聊天、帮点小忙。

规则：
1. 直接回答，用日常口气。要步骤、清单、草稿就写清楚，别绕。
2. 这一轮如果有【工具结果】，以工具结果为准，写明来源与时间；没有工具结果时，
   不要假装查过网页、官网或实时数据。
3. 普通知识、概念解释、公司介绍这类不需要实时信息的问题，直接回答，
   不要提工具、路由或「没有取到工具结果」；只有用户明确要查最新信息 / 官网 /
   实时数据、而这一轮确实没有工具结果时，才说明这次没取到并给出可执行建议。
4. 不要编造出处、网址、页码或题号；也不要说「根据资料」「原文说」。
5. 如果问题必须靠某份资料的原文才能答准，就说这条得查资料才准，别凭记忆冒充原文。
6. 不知道就说不知道，不编。
7. 默认用中文；用户换语言就跟着换。
8. 用纯文本：不要写 **、#、``` 这类 Markdown 标记；分条就用 1. 2. 3. 或短横线。
"""

GENERAL_SYSTEM = """你是「言据」的助手。这一轮已经查过资料，但没有找到能逐字引用的原文，
所以下面按通用知识回答。

规则：
1. 系统会在回答最前面加一句「资料里没有直接依据，下面按通用知识回答，不带引用。」，
   你直接从答案本身写起，不要重复这句。
2. 如果下面有【工具结果】，以工具结果为准，写明来源与时间；没有工具结果时不要假装查过网页。
3. 不要编造出处、页码、网址或题号，也不要说「根据资料」「原文说」。
4. 直接回答；不确定就说不确定，不知道就说不知道，不编。
5. 默认用中文；用户换语言就跟着换。
6. 用纯文本：不要写 **、#、``` 这类 Markdown 标记；分条就用 1. 2. 3. 或短横线。
"""


def _messages(system: str, query: str, history: Optional[List[Dict]] = None,
              style: str = "", tool_text: str = "") -> List[Dict]:
    """拼 messages。历史只留 user/assistant 的短文本。"""
    messages: List[Dict] = [{"role": "system", "content": system}]
    if style:
        messages.append({"role": "system", "content": "讲解方式：" + style[:400]})
    # 工具结果放在历史之前：它是这一轮的实时事实，不是聊天记录
    if tool_text:
        messages.append({"role": "system", "content": tool_text[:2000]})
    messages.extend(normalize_history(history, 6))
    messages.append({"role": "user", "content": (query or "").strip()})
    return messages


def build_daily_messages(query: str, history: Optional[List[Dict]] = None,
                         style: str = "", tool_text: str = "") -> List[Dict]:
    return _messages(DAILY_SYSTEM, query, history, style, tool_text)


def build_general_messages(query: str, history: Optional[List[Dict]] = None,
                           style: str = "", tool_text: str = "") -> List[Dict]:
    return _messages(GENERAL_SYSTEM, query, history, style, tool_text)


def _reply(provider, messages: List[Dict]) -> str:
    """没有可用模型时返回离线说明，不假装答过。"""
    if provider is None or not hasattr(provider, "complete"):
        return OFFLINE_REPLY
    text = provider.complete(messages, temperature=0.6, max_tokens=700,
                             json_mode=False)
    return (text or "").strip() or "（模型这次没给出内容，再说一遍试试。）"


def _stream(provider, messages: List[Dict]) -> Iterator[str]:
    """模型支持流式就走流式，否则退成一整段。"""
    if provider is not None and hasattr(provider, "chat_stream"):
        yield from provider.chat_stream(messages, temperature=0.6, max_tokens=700)
        return
    yield _reply(provider, messages)


def daily_reply(provider, query: str, history: Optional[List[Dict]] = None,
                style: str = "", tool_text: str = "") -> str:
    return _reply(provider, build_daily_messages(query, history, style, tool_text))


def general_reply(provider, query: str, history: Optional[List[Dict]] = None,
                  style: str = "", tool_text: str = "") -> str:
    """通用回答：服务端强制在前面写清「资料里没有依据」。"""
    body = _reply(provider, build_general_messages(query, history, style, tool_text))
    return GENERAL_PREFIX + strip_echo(body)


def daily_stream(provider, query: str, history: Optional[List[Dict]] = None,
                 style: str = "", tool_text: str = "") -> Iterator[str]:
    yield from _stream(provider, build_daily_messages(query, history, style, tool_text))


def general_stream(provider, query: str, history: Optional[List[Dict]] = None,
                   style: str = "", tool_text: str = "") -> Iterator[str]:
    yield GENERAL_PREFIX
    yield from _strip_echo_stream(
        _stream(provider, build_general_messages(query, history, style, tool_text)))
