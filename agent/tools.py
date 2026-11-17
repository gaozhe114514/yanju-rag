# -*- coding: utf-8 -*-
"""Agent 可调用的工具。

第一个工具 search_kb 只是把现有检索器包一层：BM25 + 向量召回 → RRF →
Cross-Encoder 精排原样复用，不在这里改分数、不改候选集。工具返回完整的
SearchHit，生成与逐字引用校验仍用同一批对象。

工具描述按 JSON Schema 写，前端 / 日志能直接展示「调了什么、参数是什么」。
"""
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from retrieval.search import SearchHit

SEARCH_KB = "search_kb"

SEARCH_KB_SCHEMA = {
    "name": SEARCH_KB,
    "description": (
        "在当前知识库里检索资料。返回带 chunk_id 与原文的 top 片段，"
        "供后续生成与逐字引用校验使用；不改变排序，也不生成答案。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要检索的独立问题，不超过 80 字"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10,
                      "description": "最多返回几条片段，默认 5"},
        },
        "required": ["query"],
    },
}


@dataclass
class ToolResult:
    name: str
    query: str
    hits: List[SearchHit] = field(default_factory=list)
    ms: int = 0
    top_k: int = 5

    def call_payload(self) -> dict:
        """写进 state.tool_calls 的可序列化记录。"""
        return {
            "name": self.name,
            "args": {"query": self.query, "top_k": self.top_k},
            "hits": len(self.hits),
            "ms": self.ms,
            "chunk_ids": [h.chunk.id for h in self.hits],
        }


class SearchKBTool:
    """search_kb 的实现：检索本身由传入的 search 函数负责。"""

    def __init__(self, search: Callable[..., List[SearchHit]], top_k: int = 5):
        self._search = search
        self.top_k = max(1, min(int(top_k or 5), 10))

    def __call__(self, query: str, top_k: Optional[int] = None,
                 skip_rerank: bool = False) -> ToolResult:
        k = self.top_k if top_k is None else max(1, min(int(top_k), 10))
        t0 = time.time()
        hits = list(self._search(query, skip_rerank=skip_rerank) or [])
        return ToolResult(SEARCH_KB, query, hits[:k],
                          int((time.time() - t0) * 1000), k)
