# -*- coding: utf-8 -*-
"""拒答闸门标定：正样本看误拒率，负样本看拒答率。只跑检索，不生成。

用法：
  py -m answer.eval_gate --positives data/eval/2019_queries_hard.jsonl \
                         --negatives data/eval/out_of_scope.jsonl \
                         --input data/work/2019_draft.jsonl \
                         --mode hybrid-ce --sweep 0.1,0.2,0.3,0.4,0.5
"""
import argparse
import glob
import json
import sys

from retrieval.chunk import load_chunks, load_knowledge_chunks
from retrieval.search import MODES, build_searcher

from .gate import RefusalGate


def load_queries(path: str):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def collect(rows, searcher):
    return [(item, searcher.search(item["query"])) for item in rows]


def refuse_rate(scored, min_score: float, min_margin: float) -> float:
    gate = RefusalGate(min_score, min_margin)
    if not scored:
        return 0.0
    n = sum(1 for _, hits in scored if gate.check("", hits).refuse)
    return n / len(scored)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--positives", nargs="+", required=True,
                    help="应当回答的 query 集（JSONL，含 gold）")
    ap.add_argument("--negatives", nargs="+", required=True,
                    help="资料外的 query 集（JSONL，无 gold）")
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--knowledge", nargs="*", default=[],
                    help="考点 JSONL；给了就把真题和考点放在同一个语料里评测")
    ap.add_argument("--mode", default="hybrid-ce", choices=list(MODES))
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--fuse-k", type=int, default=5,
                    help="精排候选数；标定 top1/top2 时 5 足够，能省大量 CPU 时间")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--min-margin", type=float, default=0.0)
    ap.add_argument("--sweep", default="",
                    help="逗号分隔的 min_score，例如 0.1,0.2,0.3")
    ap.add_argument("--sweep-margin", default="",
                    help="逗号分隔的 min_margin，与 --sweep 组合成网格")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    paths = []
    for p in args.input:
        paths.extend(sorted(glob.glob(p)) or [p])
    chunks = load_chunks(paths)
    for p in args.knowledge:
        chunks.extend(load_knowledge_chunks(p))
    searcher = build_searcher(chunks, mode=args.mode,
                              model_name=args.model,
                              reranker_model=args.reranker_model,
                              k_fuse=args.fuse_k, k_final=5)

    pos = []
    for p in args.positives:
        pos.extend(load_queries(p))
    neg = []
    for p in args.negatives:
        neg.extend(load_queries(p))
    pos_hits = collect(pos, searcher)
    neg_hits = collect(neg, searcher)

    if args.show:
        gate = RefusalGate(args.min_score, args.min_margin)
        for label, scored in (("POS", pos_hits), ("NEG", neg_hits)):
            for item, hits in scored:
                d = gate.check(item["query"], hits)
                top = hits[0].chunk.id if hits else "-"
                print("%s %s top=%s score=%.4f margin=%.4f  %s" % (
                    label, "REFUSE" if d.refuse else "PASS  ", top,
                    d.top_score, d.margin, item["query"]))

    thresholds = ([float(x) for x in args.sweep.split(",") if x.strip()]
                  if args.sweep else [args.min_score])
    margins = ([float(x) for x in args.sweep_margin.split(",") if x.strip()]
               if args.sweep_margin else [args.min_margin])
    print("min_score  min_margin  正样本误拒  负样本拒答")
    for t in thresholds:
        for m in margins:
            print("%9.4f  %10.4f  %10.3f  %10.3f" % (
                t, m, refuse_rate(pos_hits, t, m),
                refuse_rate(neg_hits, t, m)))
    print("正样本 %d 条，负样本 %d 条" % (len(pos), len(neg)))


if __name__ == "__main__":
    main()
