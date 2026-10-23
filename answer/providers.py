# -*- coding: utf-8 -*-
"""生成模型适配：DeepSeek（OpenAI 兼容）与离线抽取占位实现。"""
import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence

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

    def generate(self, messages: List[Dict], hits: Optional[Sequence[SearchHit]] = None,
                 **_) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
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
