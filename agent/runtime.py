# -*- coding: utf-8 -*-
"""Agent 运行时：线程记忆、状态图执行、SSE 事件与最终结果。

线程按 thread_id 存在 checkpointer 里；每个 thread 记一个 owner（登录用户 id
或匿名客户端 IP 哈希）。换账号或换客户端拿同一个 thread_id 续写时，会新建
线程而不是读出别人的对话。
"""
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from answer.agent import normalize_history
from retrieval.search import SearchHit

from .graph import build_agent_graph
from .nodes import AgentDeps
from .state import context_from_state, hits_from_state

_THREAD_RE = re.compile(r"[^A-Za-z0-9._-]")


def clean_thread_id(value: str) -> str:
    """只保留可安全放进 checkpoint 键的字符；空串表示让服务端生成。"""
    return _THREAD_RE.sub("", str(value or ""))[:64]


@dataclass
class AgentResult:
    refused: bool
    answer: str = ""
    citations: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    hits: List[SearchHit] = field(default_factory=list)
    context: List[SearchHit] = field(default_factory=list)
    attempts: int = 0
    errors: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    intent: str = "kb"
    rewritten: str = ""
    thread_id: str = ""
    thread_reset: bool = False
    checkpointer: str = "memory"
    timings: Dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


def _result_from_state(state: Dict[str, Any], thread_id: str, kind: str,
                       t0: float, thread_reset: bool = False) -> AgentResult:
    try:
        hits = hits_from_state(state)
    except ValueError:
        hits = []
    try:
        context = context_from_state(state)
    except ValueError:
        context = []
    return AgentResult(
        refused=bool(state.get("refused")),
        answer=str(state.get("answer") or ""),
        citations=list(state.get("citations") or []),
        reason=str(state.get("reason") or ""),
        hits=hits,
        context=context,
        attempts=int(state.get("attempt") or 0),
        errors=list(state.get("errors") or []),
        steps=list(state.get("steps") or []),
        tool_calls=list(state.get("tool_calls") or []),
        intent=str(state.get("intent") or "kb"),
        rewritten=str(state.get("search_query") or ""),
        thread_id=thread_id,
        thread_reset=thread_reset,
        checkpointer=kind,
        timings=dict(state.get("timings") or {}),
        latency_ms=int((time.time() - t0) * 1000),
    )


def _failure(reason: str, thread_id: str, kind: str, t0: float,
             steps: Optional[List[Dict[str, Any]]] = None) -> AgentResult:
    return AgentResult(
        refused=True,
        reason=reason,
        steps=steps or [],
        thread_id=thread_id,
        checkpointer=kind,
        latency_ms=int((time.time() - t0) * 1000),
    )


class AgentRuntime:
    def __init__(self, checkpointer, kind: str = "memory"):
        self.checkpointer = checkpointer
        self.kind = kind

    def _prepare(self, deps: AgentDeps, query: str, thread_id: str,
                 owner: str, history: List[Dict], style: str, collection: str):
        graph = build_agent_graph(deps, self.checkpointer)
        tid = clean_thread_id(thread_id) or uuid.uuid4().hex
        config = {"configurable": {"thread_id": tid}}
        try:
            prev = graph.get_state(config).values or {}
        except Exception:
            prev = {}
        reset = False
        exists = bool(prev.get("messages"))
        if exists and (not owner or prev.get("owner") != owner):
            # 同一个线程号被另一个账号/客户端拿到：新建线程，不读旧记忆
            tid = uuid.uuid4().hex
            config = {"configurable": {"thread_id": tid}}
            prev = {}
            exists = False
            reset = True
        seed = [] if exists else normalize_history(history, 12)
        # 当前这句永远要进 messages；已有线程靠 reducer 追加，新线程靠 seed 带入历史
        seed.append({"role": "user", "content": (query or "")[:1200]})
        input_state = {
            "messages": seed,
            "owner": owner,
            "query": query,
            "collection": collection,
            "style": (style or "")[:400],
            "intent": "",
            "search_query": "",
            "plan_note": "",
            "plan_ms": 0,
            "direct_answer": "",
            "attempt": 0,
            "hits": [],
            "tool_calls": [],
            "context": [],
            "stats": None,
            "gate_refused": False,
            "raw": "",
            "streamed": False,
            "errors": [],
            "first_errors": [],
            "stat_errors": [],
            "retry_pending": False,
            "provider_error": False,
            "refused": False,
            "answer": "",
            "citations": [],
            "reason": "",
            "steps": [],
            "timings": {},
            "done": False,
        }
        return graph, config, tid, input_state, reset

    def run(self, deps: AgentDeps, query: str, thread_id: str = "",
            owner: str = "", history: Optional[List[Dict]] = None,
            style: str = "", collection: str = "") -> AgentResult:
        t0 = time.time()
        graph, config, tid, input_state, reset = self._prepare(
            deps, query, thread_id, owner, history or [], style, collection)
        try:
            final = graph.invoke(input_state, config)
        except Exception as e:
            print("Agent 运行失败：%s: %s" % (type(e).__name__, e),
                  file=sys.stderr)
            return _failure("agent_error: %s" % type(e).__name__, tid,
                            self.kind, t0)
        return _result_from_state(final, tid, self.kind, t0, reset)

    def stream(self, deps: AgentDeps, query: str, thread_id: str = "",
               owner: str = "", history: Optional[List[Dict]] = None,
               style: str = "", collection: str = "") -> Iterator[Dict[str, Any]]:
        """先吐 status/tool/delta/reset，最后吐一个 type=final 的结果。"""
        t0 = time.time()
        graph, config, tid, input_state, reset = self._prepare(
            deps, query, thread_id, owner, history or [], style, collection)
        final_state: Dict[str, Any] = {}
        try:
            for mode, chunk in graph.stream(
                    input_state, config, stream_mode=["custom", "values"]):
                if mode == "custom":
                    yield chunk
                else:
                    final_state = chunk
        except Exception as e:
            print("Agent 流式运行失败：%s: %s" % (type(e).__name__, e),
                  file=sys.stderr)
            yield {"type": "error", "message": "Agent 运行时出错，请稍后重试"}
            yield {"type": "final",
                   "result": _failure("agent_error: %s" % type(e).__name__,
                                      tid, self.kind, t0)}
            return
        if not final_state:
            yield {"type": "error", "message": "Agent 没有返回结果"}
            yield {"type": "final",
                   "result": _failure("agent_error: empty_state", tid,
                                      self.kind, t0)}
            return
        yield {"type": "final",
               "result": _result_from_state(final_state, tid, self.kind,
                                            t0, reset)}
