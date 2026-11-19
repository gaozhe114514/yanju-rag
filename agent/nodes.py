# -*- coding: utf-8 -*-
"""状态图的节点。

节点只做一件事，且都接收 (state, deps, emit)：
- deps 里是检索器、生成器、拒答闸门和上下文准备函数，便于单测替身；
- emit 是 LangGraph 的 custom stream writer，用来把阶段、工具调用和增量文本
  推给 SSE；单测里传一个收集列表即可。

这里不 import langgraph，纯 Python 就能测。
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from answer.agent import QueryAgent, chitchat_reply
from answer.daily import daily_reply, daily_stream
from answer.aggregate import TableStats, check_counts
from answer.prompt import build_messages
from answer.providers import ProviderError, parse_payload
from answer.stream import AnswerStream, generate_chunks
from answer.verify import verify_payload
from answer.tools import run_tools as run_tool_list, tool_context as tool_context_text
from retrieval.search import SearchHit

from .prompt import build_retry_messages, refine_query
from .route import RouteDecision
from .state import (AgentState, context_from_state, hit_to_dict, hits_from_state,
                    message, stats_from_state, stats_to_dict)
from .tools import SearchKBTool

CLARIFY_TEXT = ("我不确定你指的是哪道题。可以把题号或关键词补上，"
                "例如「2019 年第 31 题」或「快速排序的时间复杂度」。")

Emit = Callable[[Dict[str, Any]], None]


@dataclass
class AgentDeps:
    """一次 Agent 运行需要的依赖；检索/生成/闸门都来自现有链路。"""

    search: Callable[..., List[SearchHit]]
    prepare: Callable[[str, List[SearchHit]],
                      Tuple[List[SearchHit], Optional[TableStats]]]
    provider: Any
    gate: Any
    agent: Any = None
    prompt_mode: str = "exam"
    top_k: int = 5
    retry: bool = True
    refiner: Optional[Callable[[str, Sequence[str], Sequence[SearchHit]],
                               Tuple[str, str]]] = None
    router: Optional[Callable] = None
    chat_reply: Optional[Callable] = None
    chat_stream: Optional[Callable] = None
    run_tools_fn: Optional[Callable] = None
    tool_context_fn: Optional[Callable] = None
    tools: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.agent is None:
            self.agent = QueryAgent(None)
        if self.chat_reply is None:
            self.chat_reply = lambda q, h, st, tt="": daily_reply(
                self.provider, q, h, st, tt)
        if self.chat_stream is None:
            self.chat_stream = lambda q, h, st, tt="": daily_stream(
                self.provider, q, h, st, tt)
        if self.run_tools_fn is None:
            self.run_tools_fn = run_tool_list
        if self.tool_context_fn is None:
            self.tool_context_fn = tool_context_text

    def route(self, query: str, history, collection: str) -> RouteDecision:
        if self.router is None:
            return RouteDecision("kb", [], "默认按资料问答")
        return self.router(query, history, collection)

    def tool(self) -> SearchKBTool:
        return SearchKBTool(self.search, self.top_k)

    def refine(self, query: str, errors: Sequence[str],
               hits: Sequence[SearchHit]) -> Tuple[str, str]:
        if self.refiner is not None:
            return self.refiner(query, errors, hits)
        return refine_query(self.provider, query, errors, hits)


def _emit(emit: Optional[Emit], event: Dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event)
    except Exception:
        pass  # 推送失败不能影响答题


def _steps(state: AgentState, name: str, ms: int,
           detail: str = "") -> List[Dict[str, Any]]:
    return list(state.get("steps") or []) + [
        {"name": name, "ms": int(ms), "detail": detail}]


def _timing(state: AgentState, key: str, ms: int) -> Dict[str, int]:
    timings = dict(state.get("timings") or {})
    timings[key] = int(timings.get(key, 0)) + int(ms)
    return timings


def plan_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """理解问题：寒暄/指代不明直接回，其余进入检索。"""
    t0 = time.time()
    query = str(state.get("query") or "")
    history = list(state.get("messages") or [])[:-1]
    plan = deps.agent.plan(query, history)
    ms = int((time.time() - t0) * 1000)
    _emit(emit, {"type": "status", "stage": "plan", "intent": plan.intent,
                 "ms": ms})

    if plan.intent == "chitchat":
        return {
            "intent": "chitchat",
            "direct_answer": chitchat_reply(query),
            "plan_ms": ms,
            "plan_note": plan.note,
            "attempt": 0,
            "steps": _steps(state, "理解问题", ms,
                            plan.note or "寒暄/元问题，不进入检索"),
            "timings": _timing(state, "plan", ms),
        }
    if plan.intent == "clarify":
        return {
            "intent": "clarify",
            "direct_answer": CLARIFY_TEXT,
            "plan_ms": ms,
            "plan_note": plan.note,
            "attempt": 0,
            "steps": _steps(state, "理解问题", ms,
                            plan.note or "指代不明，需要补充信息"),
            "timings": _timing(state, "plan", ms),
        }

    search_query = (plan.rewritten or query).strip() or query
    detail = "独立问题，直接检索"
    if plan.rewritten and plan.rewritten != query:
        detail = "结合上文改写为：%s" % plan.rewritten
    elif plan.note:
        detail = plan.note
    return {
        "intent": "kb",
        "search_query": search_query,
        "plan_note": plan.note,
        "plan_ms": ms,
        "attempt": 0,
        "steps": _steps(state, "理解问题", ms, detail),
        "timings": _timing(state, "plan", ms),
    }


def search_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """调用 search_kb：只检索，不改排序、不生成。"""
    query = str(state.get("search_query") or state.get("query") or "")
    _emit(emit, {"type": "status", "stage": "search", "query": query})
    result = deps.tool()(query)
    payload = result.call_payload()
    _emit(emit, {"type": "tool", "tool": payload})
    _emit(emit, {"type": "status", "stage": "tool", "hits": len(result.hits),
                 "query": query, "ms": result.ms})
    return {
        "hits": [hit_to_dict(h) for h in result.hits],
        "tool_calls": list(state.get("tool_calls") or []) + [payload],
        "steps": _steps(state, "工具调用 · search_kb", result.ms,
                        "%s → %d 条" % (query[:80], len(result.hits))),
        "timings": _timing(state, "retrieve", result.ms),
    }


def gate_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """拒答闸门；通过后准备交给模型的上下文（含相邻片段 / 全表统计）。"""
    t0 = time.time()
    query = str(state.get("search_query") or state.get("query") or "")
    hits = hits_from_state(state)
    decision = deps.gate.check(query, hits)
    _emit(emit, {"type": "status", "stage": "gate", "hits": len(hits),
                 "refuse": bool(decision.refuse), "reason": decision.reason})
    if decision.refuse:
        ms = int((time.time() - t0) * 1000)
        return {
            "gate_refused": True,
            "refused": True,
            "reason": decision.reason,
            "steps": _steps(state, "拒答判断", ms,
                            "未通过：%s" % (decision.reason or "分数不足")),
            "timings": _timing(state, "gate", ms),
        }
    context, stats = deps.prepare(query, hits)
    ms = int((time.time() - t0) * 1000)
    return {
        "context": [hit_to_dict(h) for h in context],
        "stats": stats_to_dict(stats),
        "steps": _steps(state, "拒答判断", ms,
                        "通过（%d 条候选）" % len(hits)),
        "timings": _timing(state, "gate", ms),
    }


def generate_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """强制 JSON 生成；流式时把 answer 增量推给前端，收完仍要校验。"""
    query = str(state.get("search_query") or state.get("query") or "")
    context = context_from_state(state)
    stats = stats_from_state(state)
    stats_text = stats.text if stats else ""
    attempt = max(1, int(state.get("attempt") or 1))
    _emit(emit, {"type": "status", "stage": "generate", "attempt": attempt})

    if attempt > 1 and state.get("first_errors"):
        messages = build_retry_messages(
            query, context, str(state.get("raw") or ""),
            list(state.get("first_errors") or []), str(state.get("style") or ""),
            deps.prompt_mode, stats_text)
    else:
        messages = build_messages(
            query, context, str(state.get("style") or ""), deps.prompt_mode,
            stats_text)

    t0 = time.time()
    buf: List[str] = []
    streamed = False
    extractor = AnswerStream()
    try:
        for chunk in generate_chunks(deps.provider, messages, context):
            buf.append(chunk)
            delta = extractor.feed(chunk)
            if delta:
                streamed = True
                _emit(emit, {"type": "delta", "text": delta})
    except Exception as e:  # ProviderError 与适配器自身的异常都按生成失败处理
        ms = int((time.time() - t0) * 1000)
        detail = str(e) if isinstance(e, ProviderError) else type(e).__name__
        _emit(emit, {"type": "error", "message": "生成服务暂时不可用"})
        return {
            "provider_error": True,
            "refused": True,
            "reason": "provider_error: %s" % detail,
            "attempt": attempt,
            "steps": _steps(state, "生成", ms, "失败：%s" % detail[:160]),
            "timings": _timing(state, "generate", ms),
        }

    ms = int((time.time() - t0) * 1000)
    return {
        "raw": "".join(buf),
        "streamed": streamed,
        "attempt": attempt,
        "steps": _steps(state, "生成", ms, "强制 JSON，第 %d 次" % attempt),
        "timings": _timing(state, "generate", ms),
    }


def verify_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """逐字引用校验；第一次不过就换查询再检索，第二次不过则拒答。"""
    t0 = time.time()
    query = str(state.get("search_query") or state.get("query") or "")
    context = context_from_state(state)
    stats = stats_from_state(state)
    raw = str(state.get("raw") or "")
    attempt = max(1, int(state.get("attempt") or 1))
    payload = None
    stat_errors: List[str] = []
    try:
        payload = parse_payload(raw)
    except ProviderError as e:
        errors = [str(e)]
    else:
        errors = verify_payload(payload, context)
        if not errors and not payload.get("refused"):
            errors = check_counts(str(payload.get("answer") or ""), stats)
            stat_errors = list(errors)

    if not errors:
        ms = int((time.time() - t0) * 1000)
        _emit(emit, {"type": "status", "stage": "verify", "ok": True,
                     "attempt": attempt})
        if payload.get("refused"):
            return {
                "refused": True,
                "reason": str(payload.get("reason") or "模型判断资料不足"),
                "attempt": attempt,
                "retry_pending": False,
                "steps": _steps(state, "引用校验", ms,
                                "模型判断资料不足：%s" %
                                (payload.get("reason") or "")[:120]),
                "timings": _timing(state, "verify", ms),
            }
        return {
            "refused": False,
            "answer": str(payload.get("answer") or ""),
            "citations": list(payload.get("citations") or []),
            "attempt": attempt,
            "errors": list(state.get("first_errors") or []),
            "retry_pending": False,
            "steps": _steps(state, "引用校验", ms, "逐字校验通过"),
            "timings": _timing(state, "verify", ms),
        }

    ms = int((time.time() - t0) * 1000)
    if attempt < 2 and deps.retry:
        new_query, source = deps.refine(query, errors, hits_from_state(state))
        note = ("统计数字和表格对不上，正在换查询重试"
                if stat_errors else "引用没有逐字对上原文，正在换查询重试")
        if state.get("streamed"):
            _emit(emit, {"type": "reset", "note": note})
        _emit(emit, {"type": "status", "stage": "retry", "from": query,
                     "to": new_query, "source": source,
                     "errors": [str(e)[:120] for e in errors[:3]]})
        detail = "未通过：%s；换查询：%s" % ("; ".join(errors)[:120], new_query[:80])
        return {
            "attempt": 2,
            "first_errors": list(state.get("first_errors") or errors),
            "errors": list(errors),
            "stat_errors": list(stat_errors),
            "retry_pending": True,
            "search_query": new_query,
            "steps": _steps(state, "换查询重试", ms, detail),
            "timings": _timing(state, "verify", ms),
        }

    reason = ("stat_failed: " if stat_errors else "citation_failed: ") + \
             "; ".join(errors)
    _emit(emit, {"type": "status", "stage": "verify", "ok": False,
                 "attempt": attempt, "errors": [str(e)[:120] for e in errors[:3]]})
    return {
        "refused": True,
        "reason": reason,
        "attempt": attempt,
        "errors": list(state.get("first_errors") or errors),
        "retry_pending": False,
        "steps": _steps(state, "引用校验", ms,
                        "未通过：%s" % "; ".join(errors)[:160]),
        "timings": _timing(state, "verify", ms),
    }


def finalize_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """成功收尾：把回答写入对话记忆，checkpoint 下一轮还能读到。"""
    answer = str(state.get("answer") or "")
    _emit(emit, {"type": "status", "stage": "done", "refused": False})
    return {
        "route": "kb",
        "messages": [message("assistant", answer)],
        "done": True,
    }


def refuse_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """拒答收尾：保留原因，写入一句简短的对话记忆。"""
    reason = str(state.get("reason") or "unknown")
    _emit(emit, {"type": "status", "stage": "refuse", "reason": reason[:160]})
    text = ("生成服务暂时不可用，已拒答。"
            if reason.startswith("provider_error")
            else "资料里没有找到可引用的依据，已拒答。")
    return {
        "route": "kb",
        "refused": True,
        "answer": "",
        "messages": [message("assistant", text)],
        "steps": _steps(state, "拒答", 0, reason[:160]),
        "done": True,
    }


def direct_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """寒暄 / 指代不明：不检索、不生成，直接给固定说明。"""
    answer = str(state.get("direct_answer") or "")
    _emit(emit, {"type": "status", "stage": "done", "refused": False})
    return {
        "route": "chat",
        "answer": answer,
        "refused": False,
        "citations": [],
        "hits": [],
        "messages": [message("assistant", answer)],
        "done": True,
    }


def route_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """决定普通对话 / 查知识库 / 调工具。"""
    t0 = time.time()
    query = str(state.get("query") or "")
    history = list(state.get("messages") or [])[:-1]
    decision = deps.route(query, history, str(state.get("collection") or ""))
    if decision.route not in ("chat", "kb", "tools"):
        decision = RouteDecision("kb", [], "路由结果不合法，按资料问答")
    if decision.route == "tools" and not decision.tools:
        decision = RouteDecision("chat", [], "没有可用工具，按普通对话回答")
    ms = int((time.time() - t0) * 1000)
    _emit(emit, {"type": "status", "stage": "route", "route": decision.route,
                 "tools": decision.tools, "reason": decision.reason})
    return {
        "route": decision.route,
        "tool_names": list(decision.tools),
        "route_reason": decision.reason,
        "steps": _steps(state, "判断回答方式", ms,
                        "%s：%s" % (decision.route, decision.reason or "自动判断")),
        "timings": _timing(state, "route", ms),
    }


def chat_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """普通对话：像日常助手一样回答，不查资料、不套引用。"""
    t0 = time.time()
    query = str(state.get("query") or "")
    history = list(state.get("messages") or [])[:-1]
    style = str(state.get("style") or "")
    _emit(emit, {"type": "status", "stage": "chat"})
    parts: List[str] = []
    try:
        for piece in deps.chat_stream(query, history, style):
            if piece:
                parts.append(piece)
                _emit(emit, {"type": "delta", "text": piece})
        answer = "".join(parts)
    except Exception as e:
        _emit(emit, {"type": "error", "message": "生成服务暂时不可用"})
        return {
            "route": "chat", "refused": True,
            "reason": "provider_error: %s" % type(e).__name__,
            "steps": _steps(state, "普通对话", int((time.time() - t0) * 1000),
                            "生成失败"),
            "done": True,
        }
    ms = int((time.time() - t0) * 1000)
    return {
        "route": "chat", "refused": False, "answer": answer,
        "citations": [], "hits": [], "context": [], "tool_results": [],
        "attempts": 0,
        "messages": [message("assistant", answer)],
        "steps": _steps(state, "普通对话", ms, "不查资料，直接回答"),
        "timings": _timing(state, "generate", ms),
        "done": True,
    }


def tools_node(state: AgentState, deps: AgentDeps, emit: Optional[Emit] = None):
    """实时 / 外部工具：先取结果，再带着工具结果回答，不冒充资料引用。"""
    t0 = time.time()
    query = str(state.get("query") or "")
    names = list(state.get("tool_names") or [])
    _emit(emit, {"type": "status", "stage": "tool", "names": names})
    results = deps.run_tools_fn(names, query, state.get("study") or {})
    for result in results:
        _emit(emit, {"type": "tool", "tool": result})
    _emit(emit, {"type": "status", "stage": "chat", "tools": [
        r.get("label") or r.get("name") for r in results]})
    context = deps.tool_context_fn(results)
    history = list(state.get("messages") or [])[:-1]
    style = str(state.get("style") or "")
    parts: List[str] = []
    try:
        for piece in deps.chat_stream(query, history, style, context):
            if piece:
                parts.append(piece)
                _emit(emit, {"type": "delta", "text": piece})
        answer = "".join(parts)
    except Exception as e:
        _emit(emit, {"type": "error", "message": "生成服务暂时不可用"})
        return {
            "route": "tools", "refused": True,
            "reason": "provider_error: %s" % type(e).__name__,
            "tool_results": results,
            "steps": _steps(state, "工具调用", int((time.time() - t0) * 1000),
                            "工具结果已取到，生成失败"),
            "done": True,
        }
    ms = int((time.time() - t0) * 1000)
    return {
        "route": "tools", "refused": False, "answer": answer,
        "citations": [], "hits": [], "context": [],
        "tool_results": results, "attempts": 0,
        "messages": [message("assistant", answer)],
        "steps": _steps(state, "工具回答", ms,
                        "工具：%s" % "、".join(
                            str(r.get("label") or r.get("name") or "工具")
                            for r in results)),
        "timings": _timing(state, "generate", ms),
        "done": True,
    }


def after_plan(state: AgentState) -> str:
    return "direct" if state.get("intent") in ("chitchat", "clarify") else "route"


def after_route(state: AgentState) -> str:
    route = state.get("route")
    if route == "chat":
        return "chat"
    if route == "tools":
        return "tools"
    return "search"


def after_gate(state: AgentState) -> str:
    return "refuse" if state.get("gate_refused") else "generate"


def after_generate(state: AgentState) -> str:
    return "refuse" if state.get("provider_error") else "verify"


def after_verify(state: AgentState) -> str:
    if state.get("retry_pending"):
        return "retry"
    return "refuse" if state.get("refused") else "finalize"
