# -*- coding: utf-8 -*-
"""命令行检索：py -m retrieval.cli "二叉排序树的中序遍历" --input data/work/2019_draft.jsonl"""
import argparse
import glob
import sys

from .chunk import load_chunks
from .search import MODES, build_searcher


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--input", nargs="+",
                    default=["data/work/2019_draft.jsonl"])
    ap.add_argument("--mode", default="hybrid", choices=list(MODES))
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    paths = []
    for p in args.input:
        paths.extend(sorted(glob.glob(p)) or [p])
    chunks = load_chunks(paths)
    searcher = build_searcher(chunks, mode=args.mode, model_name=args.model,
                              reranker_model=args.reranker_model,
                              k_final=args.top_k)
    hits = searcher.search(args.query)
    print("query: %s  (%s, %d chunks)" % (args.query, args.mode, len(chunks)))
    for i, h in enumerate(hits, 1):
        c = h.chunk
        print("\n%d. %s  score=%.3f  bm25=%.2f  vec=%.3f  [%s]" % (
            i, c.id, h.score, h.bm25, h.vector, c.subject or "?"))
        print("   " + " ".join(c.text.split())[:160])
        if c.answer:
            print("   答案: %s" % c.answer)
        ref = c.source_ref or {}
        if ref.get("file"):
            print("   来源: %s 第 %s 行" % (ref.get("file"), ref.get("line")))


if __name__ == "__main__":
    main()
