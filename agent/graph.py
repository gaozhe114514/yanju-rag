# -*- coding: utf-8 -*-
"""LangGraph 状态图：规划 → search_kb → 闸门 → 生成 → 校验 → 重试/拒答。

只在这里 import langgraph；节点与工具保持框架无关，单测可以不拉起图。
图是有界的：引用校验最多重试一次（换查询再检索），之后必须拒答。
"""
from typing import Any, Optional

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from .nodes import (AgentDeps, after_generate, after_gate, after_plan,
                    after_route, after_verify, chat_node, direct_node,
                    finalize_node, gate_node, generate_node, plan_node,
                    refuse_node, route_node, search_node, tools_node,
                    verify_node)
from .state import AgentState


def _wrap(fn, deps: AgentDeps):
    def node(state):
        try:
            emit = get_stream_writer()
        except Exception:
            emit = None
        return fn(state, deps, emit)
    return node


def build_agent_graph(deps: AgentDeps, checkpointer: Optional[Any] = None):
    graph = StateGraph(AgentState)
    graph.add_node("plan", _wrap(plan_node, deps))
    graph.add_node("direct", _wrap(direct_node, deps))
    graph.add_node("route", _wrap(route_node, deps))
    graph.add_node("chat", _wrap(chat_node, deps))
    graph.add_node("tools", _wrap(tools_node, deps))
    graph.add_node("search_kb", _wrap(search_node, deps))
    graph.add_node("gate", _wrap(gate_node, deps))
    graph.add_node("generate", _wrap(generate_node, deps))
    graph.add_node("verify", _wrap(verify_node, deps))
    graph.add_node("finalize", _wrap(finalize_node, deps))
    graph.add_node("refuse", _wrap(refuse_node, deps))

    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", after_plan,
                                {"direct": "direct", "route": "route"})
    graph.add_edge("direct", END)
    graph.add_conditional_edges("route", after_route,
                                {"chat": "chat", "tools": "tools",
                                 "search": "search_kb"})
    graph.add_edge("chat", END)
    graph.add_edge("tools", END)
    graph.add_edge("search_kb", "gate")
    graph.add_conditional_edges("gate", after_gate,
                                {"generate": "generate", "refuse": "refuse"})
    graph.add_conditional_edges("generate", after_generate,
                                {"verify": "verify", "refuse": "refuse"})
    graph.add_conditional_edges("verify", after_verify,
                                {"retry": "search_kb", "finalize": "finalize",
                                 "refuse": "refuse"})
    graph.add_edge("finalize", END)
    graph.add_edge("refuse", END)
    return graph.compile(checkpointer=checkpointer)
