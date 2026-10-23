# -*- coding: utf-8 -*-
"""检索评测：Recall@k / MRR。

查询集格式（JSONL）：{"query": "...", "gold": "2019-408-01", "note": "可选"}
"""
import argparse
import json
import sys
from typing import Dict, List

from .chunk import load_chunks
from .search import MODES, HybridSearcher, build_searcher


def evaluate(searcher: HybridSearcher, items: List[Dict], k: int = 5) -> Dict:
    hits_at_1 = hits_at_k = 0
    rr = 0.0
    misses = []
    for it in items:
        ids = [h.chunk.id for h in searcher.search(it["query"])]
        gold = it["gold"]
        if gold in ids[:1]:
            hits_at_1 += 1
        if gold in ids[:k]:
            hits_at_k += 1
        else:
            misses.append((it["query"], gold, ids[:k]))
        if gold in ids:
            rr += 1.0 / (ids.index(gold) + 1)
    n = len(items) or 1
    return {
        "n": len(items),
        "recall@1": hits_at_1 / n,
        "recall@%d" % k: hits_at_k / n,
        "mrr": rr / n,
        "misses": misses,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--mode", default="hybrid", choices=list(MODES))
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()

    chunks = load_chunks(args.input)
    queries = [json.loads(l) for l in open(args.queries, encoding="utf-8") if l.strip()]
    searcher = build_searcher(chunks, mode=args.mode, model_name=args.model,
                              reranker_model=args.reranker_model, k_final=args.k)
    res = evaluate(searcher, queries, k=args.k)
    print("mode=%s  chunks=%d  queries=%d" % (args.mode, len(chunks), res["n"]))
    print("Recall@1=%.3f  Recall@%d=%.3f  MRR=%.3f" % (
        res["recall@1"], args.k, res["recall@%d" % args.k], res["mrr"]))
    if args.show_misses:
        for q, gold, got in res["misses"]:
            print("MISS  gold=%s  got=%s  q=%s" % (gold, ",".join(got), q))


if __name__ == "__main__":
    main()
