# -*- coding: utf-8 -*-
"""生成模型适配：DeepSeek（OpenAI 兼容）与离线抽取占位实现。"""
import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, Iterator, List, Optional, Sequence

from retrieval.search import SearchHit


class ProviderError(RuntimeError):
    pass


def parse_payload(text: str) -> Dict:
    """从模型输出里取出 JSON 对象，容忍 ```json 围栏和前后废话。"""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < start:
        raise ProviderError("输出里找不到 JSON 对象")
    try:
        obj = json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        raise ProviderError("JSON 解析失败：%s" % e)
    if not isinstance(obj, dict):
        raise ProviderError("JSON 顶层不是对象")
    return obj


class ExtractiveProvider:
    """没有 API key 时的占位实现：直接回 top-1 的答案。

    它不做任何推理，只用于打通「检索 → JSON → 引用校验」链路和跑单测，
    不能当作生成模型评估答案质量。
    """

    def generate(self, messages: List[Dict], hits: Optional[Sequence[SearchHit]] = None,
                 **_) -> str:
        if not hits:
            return json.dumps({"refused": True, "answer": "", "citations": [],
                               "reason": "没有检索到资料"}, ensure_ascii=False)
        c = hits[0].chunk
        quote = c.text.strip()[:60]
        if len(quote) < 6:
            return json.dumps({"refused": True, "answer": "", "citations": [],
                               "reason": "命中资料过短，无法引用"}, ensure_ascii=False)
        return json.dumps({
            "refused": False,
            "answer": "最相关的题目是 %s，参考答案：%s" % (c.id, c.answer or "见原文"),
            "citations": [{"chunk_id": c.id, "quote": quote}],
            "reason": "",
        }, ensure_ascii=False)

    def generate_stream(self, messages: List[Dict],
                        hits: Optional[Sequence[SearchHit]] = None,
                        **_) -> Iterator[str]:
        """离线实现没有真正的增量，一次给出完整 JSON。"""
        yield self.generate(messages, hits=hits)


class DeepSeekProvider:
    """DeepSeek Chat Completions，OpenAI 兼容协议，只用标准库。"""

    def __init__(self, api_key: str = "", model: str = "deepseek-chat",
                 base_url: str = "", timeout: int = 60,
                 temperature: float = 0.2, max_tokens: int = 800):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ProviderError("未设置 DEEPSEEK_API_KEY")
        self.model = model
        self.base_url = (base_url or os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _post(self, body: Dict) -> Dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise ProviderError("DeepSeek HTTP %d：%s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError) as e:
            raise ProviderError("DeepSeek 请求失败：%s" % e)

    def complete(self, messages: List[Dict], temperature: float = 0.0,
                 max_tokens: int = 200, json_mode: bool = False) -> str:
        """通用补全，供查询改写等轻量任务使用。"""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last = None
        for attempt in range(2):
            try:
                resp = self._post(body)
                try:
                    return resp["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    raise ProviderError("DeepSeek 响应格式异常：%s" % e)
            except ProviderError as e:
                last = e
                if attempt == 0:
                    time.sleep(1.0)
        raise last

    def chat_tools(self, messages: List[Dict], tools: List[Dict],
                   temperature: float = 0.2,
                   max_tokens: int = 800,
                   tool_choice: str = "auto") -> Dict:
        """原生函数调用：返回 assistant message（可能带 tool_calls）。"""
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last = None
        for attempt in range(2):
            try:
                resp = self._post(body)
                msg = resp["choices"][0]["message"]
                return {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": list(msg.get("tool_calls") or []),
                }
            except (ProviderError, KeyError, IndexError, TypeError) as e:
                last = e if isinstance(e, ProviderError) else ProviderError(
                    "DeepSeek 工具调用响应异常：%s" % e)
                if attempt == 0:
                    time.sleep(1.0)
        raise last

    def generate(self, messages: List[Dict], hits: Optional[Sequence[SearchHit]] = None,
                 **_) -> str:
        return self.complete(messages, temperature=self.temperature,
                             max_tokens=self.max_tokens, json_mode=True)

    def chat_stream(self, messages: List[Dict], temperature: float = 0.6,
                    max_tokens: int = 800) -> Iterator[str]:
        """日常对话的流式补全：纯文本，不开 JSON 模式。"""
        yield from self._post_stream({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        })

    def _post_stream(self, body: Dict) -> Iterator[str]:
        """按 SSE 读取 chat/completions，逐段吐出 delta.content。"""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=data,
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "Authorization": "Bearer " + self.api_key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = obj["choices"][0].get("delta", {}).get("content") or ""
                    except (KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        yield delta
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise ProviderError("DeepSeek HTTP %d：%s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError) as e:
            raise ProviderError("DeepSeek 流式请求失败：%s" % e)

    def generate_stream(self, messages: List[Dict],
                        hits: Optional[Sequence[SearchHit]] = None,
                        **_) -> Iterator[str]:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "response_format": {"type": "json_object"},
        }
        yield from self._post_stream(body)
