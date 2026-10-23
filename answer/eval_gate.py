# -*- coding: utf-8 -*-
"""拒答闸门标定：正样本看误拒率，负样本看拒答率。只跑检索，不生成。

用法：
  py -m answer.eval_gate --positives data/eval/2019_queries_hard.jsonl \
                         --negatives data/eval/out_of_scope.jsonl \
                         --input data/work/2019_draft.jsonl \
                         --mode hybrid-ce --min-score 0.3
"""
import argparse
import glob
import json
import sys

from retrieval.chunk import load_chunks
from retrieval.search import MODES, build_searcher

from .gate import RefusalGate


def load_queries(path: str):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rate(rows, gate, searcher, show):
    refused = 0
    for item in rows:
        hits = searcher.search(item["query"])
        d = gate.check(item["query"], hits)
        if d.refuse:
            refused += 1
        if show:
            top = hits[0].chunk.id if hits else "-"
            print("%s  top=%s  score=%.4f  margin=%.4f  %s" % (
                "REFUSE" if d.refuse else "PASS  ", top, d.top_score,
                d.margin, item["query"]))
    return refused, len(rows)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--positives", nargs="+", required=True,
                    help="应当回答的 query 集（JSONL，含 gold）")
    ap.add_argument("--negatives", nargs="+", required=True,
                    help="资料外的 query 集（JSONL，无 gold）")
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--mode", default="hybrid-ce", choices=list(MODES))
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--min-margin", type=float, default=0.0)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    paths = []
    for p in args.input:
        paths.extend(sorted(glob.glob(p)) or [p])
    searcher = build_searcher(load_chunks(paths), mode=args.mode,
                              model_name=args.model,
                              reranker_model=args.reranker_model, k_final=5)
    gate = RefusalGate(args.min_score, args.min_margin)

    pos = []
    for p in args.positives:
        pos.extend(load_queries(p))
    neg = []
    for p in args.negatives:
        neg.extend(load_queries(p))

    pos_ref, pos_n = rate(pos, gate, searcher, args.show)
    neg_ref, neg_n = rate(neg, gate, searcher, args.show)
    print("min_score=%.3f  min_margin=%.3f" % (args.min_score, args.min_margin))
    print("正样本误拒：%d/%d = %.3f" % (pos_ref, pos_n, pos_ref / (pos_n or 1)))
    print("负样本拒答：%d/%d = %.3f" % (neg_ref, neg_n, neg_ref / (neg_n or 1)))


if __name__ == "__main__":
    main()
