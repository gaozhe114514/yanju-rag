# -*- coding: utf-8 -*-
"""多轮问答的轻量 Agent：意图识别 + 追问改写。

职责很窄：把「那时间复杂度呢」这类依赖上文的追问，补全成一个可以独立检索的
问题；纯寒暄不进入检索，指代不明时让用户补一句。真正的回答仍然走
AnswerPipeline，引用校验和拒答规则不变。
"""
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .providers import parse_payload

# 只有整句都是寒暄/元问题才走闲聊分支，避免把「什么是中断」误判成闲聊
_CHITCHAT_RE = re.compile(
    r"^(你好|您好|hi|hello|嗨|在吗|谢谢|感谢|多谢|再见|拜拜|"
    r"你是谁|你能做什么|你会什么|怎么用|使用说明|帮助)"
    r"[\s!！。.？?~～]*$",
    re.IGNORECASE,
)

# 出现这些词时，问题很可能依赖上一轮，值得花一次模型调用做改写
_CONTEXT_CUES = (
    "它", "他", "她", "其", "该", "此", "这题", "那题", "这道", "那道",
    "上述", "前面", "刚刚", "刚才", "上一", "还有", "另外", "那么", "那",
    "呢", "为什么", "怎么", "如何",
)

_INTRO = (
    "我是「言据」的资料问答助手，负责从资料原文里找依据并给出可核对的引用。"
    "你可以直接问题目或知识点，例如「CSMA/CD 最小帧长怎么算」。"
    "资料没覆盖的内容我会明确拒答，不硬编。"
)


@dataclass
class AgentPlan:
    intent: str = "kb"          # kb / chitchat / clarify
    query: str = ""             # 用户原话
    rewritten: str = ""         # 真正拿去检索的问题
    source: str = "none"        # none / rule / llm
    note: str = ""
    ms: int = 0

    def to_dict(self) -> Dict:
        return {
            "intent": self.intent,
            "query": self.query,
            "rewritten": self.rewritten,
            "source": self.source,
            "note": self.note,
            "ms": self.ms,
        }


def is_chitchat(query: str) -> bool:
    return bool(_CHITCHAT_RE.match((query or "").strip()))


def chitchat_reply(query: str) -> str:
    q = (query or "").strip()
    if any(w in q for w in ("谢谢", "感谢", "多谢")):
        return "不客气。还有哪道题想一起看？"
    if any(w in q for w in ("再见", "拜拜")):
        return "好，随时回来刷题。"
    return _INTRO


def normalize_history(history: Optional[List[Dict]], limit: int = 6) -> List[Dict]:
    """只保留 user/assistant 的短文本，防止前端塞进超长内容。"""
    out: List[Dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content[:1200]})
    return out[-limit:]


def _needs_rewrite(query: str) -> bool:
    q = (query or "").strip()
    return any(cue in q for cue in _CONTEXT_CUES)


def _rule_rewrite(query: str, history: List[Dict]) -> str:
    last_user = ""
    for m in reversed(history):
        if m["role"] == "user":
            last_user = m["content"].replace("\n", " ")[:120]
            break
    return (last_user + " " + query).strip() if last_user else query


_UNDERSTAND_SYSTEM = """你是「言据」资料问答系统的查询理解模块。
根据对话历史，把用户最后一句话改写成不依赖上文、可直接检索的独立问题。

只输出 JSON：
{"intent":"kb"|"clarify","query":"改写后的问题","reason":"一句话说明"}

规则：
1. 追问里的“它 / 这题 / 那为什么 / 还有呢”等指代，用历史补全；只改写，不回答问题。
2. 不要加入历史里没有的事实、年份或题号。
3. 如果无法判断指代对象，intent 用 clarify，query 留空。
4. 本身就是独立问题时，query 保持原样。
5. query 不超过 80 字，保留专业术语和中英文缩写。
"""


class QueryAgent:
    def __init__(self, provider=None, max_history: int = 6):
        self.provider = provider
        self.max_history = max_history

    def plan(self, query: str, history: Optional[List[Dict]] = None) -> AgentPlan:
        t0 = time.time()
        q = (query or "").strip()
        hist = normalize_history(history, self.max_history)[-4:]

        if is_chitchat(q):
            return AgentPlan("chitchat", q, q, "rule", "寒暄/元问题，不检索", _ms(t0))

        if not hist or not _needs_rewrite(q):
            return AgentPlan("kb", q, q, "none", "独立问题，无需改写", _ms(t0))

        note = ""
        if self.provider is not None and hasattr(self.provider, "complete"):
            try:
                plan = self._llm_plan(q, hist)
                plan.ms = _ms(t0)
                return plan
            except Exception as e:  # 模型改写失败不能影响答题，退回规则
                note = "模型改写失败，退回规则：%s" % str(e)[:120]

        rewritten = _rule_rewrite(q, hist)
        return AgentPlan("kb", q, rewritten, "rule",
                         note or "按规则拼接上一轮问题", _ms(t0))

    def _llm_plan(self, query: str, history: List[Dict]) -> AgentPlan:
        lines = []
        for m in history:
            who = "用户" if m["role"] == "user" else "助手"
            lines.append("%s：%s" % (who, m["content"][:300]))
        user = "对话历史：\n%s\n\n用户最新问题：%s" % ("\n".join(lines), query)
        raw = self.provider.complete(
            [{"role": "system", "content": _UNDERSTAND_SYSTEM},
             {"role": "user", "content": user}],
            temperature=0.0, max_tokens=220, json_mode=True)
        obj = parse_payload(raw)
        intent = "clarify" if obj.get("intent") == "clarify" else "kb"
        rewritten = (obj.get("query") or "").strip()
        reason = (obj.get("reason") or "").strip()[:120]
        if intent == "clarify":
            return AgentPlan("clarify", query, query, "llm",
                             reason or "无法判断指代对象")
        if not rewritten:
            return AgentPlan("kb", query, query, "llm",
                             reason or "模型未给出改写，保留原问题")
        return AgentPlan("kb", query, rewritten, "llm", reason)


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
