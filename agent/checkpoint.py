# -*- coding: utf-8 -*-
"""Agent 记忆的存放位置。

- 有 DATABASE_URL 且装了 langgraph-checkpoint-postgres：用 PostgresSaver，
  表建在同一个 PostgreSQL 16 里，重启不丢线程；
- 其余情况：InMemorySaver，只活在这个进程里，适合本地 mock 与单测。

任何一步失败都退回内存，不让 Agent 因为记忆后端不可用而起不来。
"""
import sys
from dataclasses import dataclass
from typing import Any


@dataclass
class CheckpointHandle:
    saver: Any
    kind: str
    conn: Any = None

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def _memory() -> CheckpointHandle:
    from langgraph.checkpoint.memory import InMemorySaver
    return CheckpointHandle(InMemorySaver(), "memory")


def build_checkpointer(database_url: str = "") -> CheckpointHandle:
    """优先 Postgres，失败退回内存；kind 会出现在 /agent 响应与 /health。"""
    if not database_url:
        return _memory()
    conn = None
    try:
        import psycopg
        from psycopg.rows import dict_row
        from langgraph.checkpoint.postgres import PostgresSaver

        conn = psycopg.connect(database_url, autocommit=True,
                               prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()
        return CheckpointHandle(saver, "postgres", conn)
    except Exception as e:  # 没装包 / 连不上 / 没建表权限都退回内存
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        print("Agent 记忆退回内存：%s" % e, file=sys.stderr)
        return _memory()
