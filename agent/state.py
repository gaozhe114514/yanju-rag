# -*- coding: utf-8 -*-
"""Agent 状态：只放可 JSON 序列化的东西，checkpoint 才能写进 Postgres。

SearchHit / Chunk 是 dataclass，直接塞进状态会让 PostgresSaver 的序列化器为难；
这里统一转成 dict，节点要用时再还原成对象。messages 用带上限的 reducer，
多轮对话只保留最近 20 条，checkpoint 不会无限长。
"""
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from answer.aggregate import TableStats
from retrieval.chunk import Chunk
from retrieval.search import SearchHit

MAX_MESSAGES = 20


def trim_messages(left: Optional[List[Dict]], right: Optional[List[Dict]]):
    """把两条消息列表接起来，只留最后 MAX_MESSAGES 条。"""
    merged = list(left or []) + list(right or [])
    return merged[-MAX_MESSAGES:]


class AgentState(TypedDict, total=False):
    # 对话记忆：user / assistant 的短文本；由 checkpointer 按 thread_id 持久化
    messages: Annotated[List[Dict[str, Any]], trim_messages]
    # 线程归属：换账号或换 IP 的匿名客户端不能续写别人的线程
    owner: str
    query: str
    collection: str
    style: str

    # plan 节点的产出
    intent: str                 # kb / chitchat / clarify
    search_query: str
    plan_note: str
    plan_ms: int
    direct_answer: str

    # search_kb 工具的产出
    attempt: int
    hits: List[Dict[str, Any]]
    tool_calls: List[Dict[str, Any]]

    # gate 节点补充的生成上下文
    context: List[Dict[str, Any]]
    stats: Optional[Dict[str, Any]]
    gate_refused: bool

    # 生成 / 校验
    raw: str
    streamed: bool
    errors: List[str]
    first_errors: List[str]
    stat_errors: List[str]
    retry_pending: bool
    provider_error: bool
    refused: bool
    answer: str
    citations: List[Dict[str, Any]]
    reason: str

    # 可观测性
    steps: List[Dict[str, Any]]
    timings: Dict[str, int]
    done: bool


_CHUNK_FIELDS = ("id", "seq", "text", "answer", "analysis", "subject",
                 "kp_ids", "source_ref", "kind", "title", "chapter")


def hit_to_dict(hit: SearchHit) -> Dict[str, Any]:
    return {
        "chunk": hit.chunk.to_dict(),
        "score": float(hit.score),
        "bm25": float(getattr(hit, "bm25", 0.0) or 0.0),
        "vector": float(getattr(hit, "vector", 0.0) or 0.0),
    }


def hit_from_dict(data: Dict[str, Any]) -> SearchHit:
    raw = dict(data.get("chunk") or {})
    missing = [k for k in ("id", "seq", "text") if k not in raw]
    if missing:
        raise ValueError("checkpoint 里的 chunk 缺少字段：%s" % ", ".join(missing))
    chunk = Chunk(**{k: raw.get(k) for k in _CHUNK_FIELDS if k in raw})
    return SearchHit(chunk, float(data.get("score") or 0.0),
                     float(data.get("bm25") or 0.0),
                     float(data.get("vector") or 0.0))


def hits_from_state(state: AgentState) -> List[SearchHit]:
    return [hit_from_dict(d) for d in (state.get("hits") or [])]


def context_from_state(state: AgentState) -> List[SearchHit]:
    return [hit_from_dict(d) for d in (state.get("context") or [])]


def stats_to_dict(stats: Optional[TableStats]) -> Optional[Dict[str, Any]]:
    if stats is None:
        return None
    return {"text": stats.text, "numbers": list(stats.numbers),
            "partial": bool(stats.partial)}


def stats_from_state(state: AgentState) -> Optional[TableStats]:
    data = state.get("stats")
    if not data:
        return None
    return TableStats(text=str(data.get("text") or ""),
                      numbers=[int(n) for n in (data.get("numbers") or [])],
                      partial=bool(data.get("partial")))


def message(role: str, content: str) -> Dict[str, str]:
    """对话记忆只留短文本，避免把整篇资料塞进 checkpoint。"""
    return {"role": role, "content": (content or "")[:1200]}
