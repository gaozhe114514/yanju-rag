# -*- coding: utf-8 -*-
"""把 JSONL 语料写进 Postgres（运维/初始化用）。

镜像启动时 api.app 会自己同步；这个脚本用于手工重建、核对数据：
  DATABASE_URL=postgresql://... python -m db.load
"""
import argparse
import os
import sys
from pathlib import Path

from api.knowledge import load_knowledge
from db.store import Store
from retrieval.chunk import build_chunk, build_knowledge_chunk, load_questions
from retrieval.vector import BgeEmbedder

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    "data/work/2019_draft.jsonl,data/work/2020_draft.jsonl,"
    "data/work/2021_draft.jsonl,data/work/2023_draft.jsonl,"
    "data/work/2024_draft.jsonl")


def _resolve(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else ROOT / p)


def main() -> None:
    ap = argparse.ArgumentParser(description="同步题库到 PostgreSQL")
    ap.add_argument("--input", default=DEFAULT_INPUT, help="题目 JSONL，逗号分隔")
    ap.add_argument("--knowledge", default="data/curated/knowledge.jsonl")
    ap.add_argument("--no-embed", action="store_true", help="只写文本，不写向量")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        sys.exit("请先设置 DATABASE_URL")
    store = Store.from_env()
    store.init_schema()

    paths = [_resolve(p.strip()) for p in args.input.split(",") if p.strip()]
    questions = load_questions(paths)
    knowledge = load_knowledge(_resolve(args.knowledge), args.knowledge)
    chunks = [build_chunk(q) for q in questions]
    chunks.extend(build_knowledge_chunk(c, c["_file"], c["_line"])
                  for c in knowledge)

    embedder = None if args.no_embed else BgeEmbedder()
    stats = store.sync(questions, knowledge, chunks, embedder=embedder,
                       model_name=embedder.model_name if embedder else "")
    print("题目 %d / 考点 %d / %s" % (
        stats["questions"], stats["knowledge"],
        "已重建" if stats["reloaded"] else "指纹未变，跳过"))


if __name__ == "__main__":
    main()
