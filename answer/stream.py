# -*- coding: utf-8 -*-
"""流式生成的小工具：增量取出 JSON 里的 answer 字段。

生成端仍然只输出 JSON（引用校验依赖完整的 JSON），但 JSON 流式返回时
`answer` 字段是普通字符串，可以边收边解码给前端看。其余字段（引用、
refused、reason）要等整段 JSON 收完、校验通过后才会出现在 done 事件里。

所以流式只负责「让回答先长出来」，最终能不能留在这条消息里，仍以
verify_payload 的结果为准：校验失败会发 reset 事件让前端清空重来。
"""
import json
import re
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from retrieval.search import SearchHit

_ANSWER_KEY = re.compile(r'"answer"\s*:\s*"')
_HEX4 = re.compile(r"^[0-9a-fA-F]{4}$")

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def extract_answer(buffer: str) -> Tuple[str, bool]:
    """从尚未闭合的 JSON 文本里取出 answer 字符串。

    返回 (已解码的文本, 字符串是否已经闭合)。没有遇到 `"answer": "` 时返回 ("", False)。
    解码到一半遇到转义序列不完整（例如只收到 `\\u4e2`）就停下，等下一块数据。
    """
    m = _ANSWER_KEY.search(buffer or "")
    if not m:
        return "", False
    out: List[str] = []
    i = m.end()
    n = len(buffer)
    while i < n:
        ch = buffer[i]
        if ch == '"':
            return "".join(out), True
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break
        esc = buffer[i + 1]
        if esc == "u":
            code = buffer[i + 2:i + 6]
            if len(code) < 4 or not _HEX4.match(code):
                break
            point = int(code, 16)
            i += 6
            # 代理对（😀 这种）要等后半个也到齐再合并
            if 0xD800 <= point <= 0xDBFF:
                if i + 6 > n:
                    break
                low = buffer[i:i + 2]
                code2 = buffer[i + 2:i + 6]
                if low == "\\u" and _HEX4.match(code2):
                    low_point = int(code2, 16)
                    if 0xDC00 <= low_point <= 0xDFFF:
                        point = 0x10000 + ((point - 0xD800) << 10) + (low_point - 0xDC00)
                        i += 6
            out.append(chr(point))
            continue
        if esc in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[esc])
            i += 2
            continue
        # 非法转义：原样保留，别把已经出来的内容吞掉
        out.append(esc)
        i += 2
    return "".join(out), False


class AnswerStream:
    """逐块喂 JSON 文本，返回新增的 answer 片段。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._emitted = 0
        self.closed = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buffer += chunk
        text, closed = extract_answer(self._buffer)
        self.closed = self.closed or closed
        if len(text) <= self._emitted:
            return ""
        delta = text[self._emitted:]
        self._emitted = len(text)
        return delta


def generate_chunks(provider, messages: List[Dict],
                    hits: Optional[Sequence[SearchHit]] = None) -> Iterator[str]:
    """优先用 provider 的流式接口；没有就退回一次性生成。"""
    stream = getattr(provider, "generate_stream", None)
    if callable(stream):
        yield from stream(messages, hits=hits)
        return
    yield provider.generate(messages, hits=hits)


def sse(event: Dict) -> str:
    """把事件编码成 Server-Sent Events 的一帧。"""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
