# -*- coding: utf-8 -*-
"""生成评测：真实模型下的拒答分布、引用一次通过率与延迟。

用法：
  py -m answer.eval_answer --queries data/eval/2019_queries_hard.jsonl \
      --negatives data/eval/out_of_scope.jsonl \
      --input data/work/2019_draft.jsonl --mode hybrid-ce \
      --provider deepseek --fuse-k 10 --min-score 0.05 --min-margin 0.05
"""
import argparse
import glob
import json
import sys
import time

from retrieval.chunk import load_chunks
from retrieval.search import MODES, build_searcher

from .gate import RefusalGate
from .pipeline import AnswerPipeline
from .providers import DeepSeekProvider, ExtractiveProvider, ProviderError


def load_queries(path: str):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", required=True)
    ap.add_argument("--negatives", nargs="*", default=[])
    ap.add_argument("--input", nargs="+", required=True)
    ap.add_argument("--mode", default="hybrid-ce", choices=list(MODES))
    ap.add_argument("--provider", default="deepseek", choices=["mock", "deepseek"])
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--fuse-k", type=int, default=10)
    ap.add_argument("--min-score", type=float, default=0.05)
    ap.add_argument("--min-margin", type=float, default=0.05)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，用于冒烟")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    paths = []
    for p in args.input:
        paths.extend(sorted(glob.glob(p)) or [p])
    searcher = build_searcher(load_chunks(paths), mode=args.mode,
                              model_name=args.model,
                              reranker_model=args.reranker_model,
                              k_fuse=args.fuse_k, k_final=5)
    if args.provider == "deepseek":
        try:
            provider = DeepSeekProvider()
        except ProviderError as e:
            print("provider 初始化失败：%s" % e)
            sys.exit(2)
    else:
        provider = ExtractiveProvider()

    gate = RefusalGate(args.min_score, args.min_margin)
    pipeline = AnswerPipeline(searcher, provider, gate=gate)

    rows = []
    for p in args.queries:
        rows.extend(load_queries(p))
    if args.limit:
        rows = rows[:args.limit]

    stats = {"first": 0, "retry": 0, "gate_ref": 0, "model_ref": 0,
             "cite_fail": 0, "provider_err": 0}
    latencies = []
    attempts_total = 0
    errors: dict = {}
    t_all = time.time()
    for item in rows:
        t0 = time.time()
        res = pipeline.answer(item["query"])
        dt = time.time() - t0
        latencies.append(dt)
        attempts_total += res.attempts
        if not res.refused:
            stats["retry" if res.attempts > 1 else "first"] += 1
        elif res.attempts == 0:
            stats["gate_ref"] += 1
        elif res.reason.startswith("provider_error"):
            stats["provider_err"] += 1
        elif res.reason.startswith("citation_failed"):
            stats["cite_fail"] += 1
        else:
            stats["model_ref"] += 1
        for e in res.errors:
            errors[e] = errors.get(e, 0) + 1
        if args.show:
            print("%s attempts=%d %.1fs %s" % (
                "REFUSE" if res.refused else "OK    ", res.attempts, dt,
                item["query"]))
            if res.refused:
                print("    " + res.reason)

    n = len(rows) or 1
    answered = stats["first"] + stats["retry"]
    print("\n样本 %d 条，总耗时 %.1fs" % (len(rows), time.time() - t_all))
    print("作答 %d（一次通过 %d，重试后通过 %d）" % (
        answered, stats["first"], stats["retry"]))
    print("拒答 %d（闸门 %d，模型判断 %d，引用失败 %d，provider 错误 %d）" % (
        stats["gate_ref"] + stats["model_ref"] + stats["cite_fail"]
        + stats["provider_err"],
        stats["gate_ref"], stats["model_ref"], stats["cite_fail"],
        stats["provider_err"]))
    gen_n = stats["first"] + stats["retry"] + stats["cite_fail"]
    print("首次引用通过率 %.3f（进入生成的 %d 条中）  最终作答率 %.3f  平均尝试 %.2f" % (
        stats["first"] / (gen_n or 1), gen_n, answered / n,
        attempts_total / n))
    print("平均延迟 %.1fs" % (sum(latencies) / (len(latencies) or 1)))
    if errors:
        print("首次校验失败原因 top:")
        for e, c in sorted(errors.items(), key=lambda x: -x[1])[:5]:
            print("  %dx %s" % (c, e))

    if args.negatives:
        neg = []
        for p in args.negatives:
            neg.extend(load_queries(p))
        refused = 0
        for item in neg:
            hits = searcher.search(item["query"])
            if gate.check(item["query"], hits).refuse:
                refused += 1
        print("负样本拒答 %d/%d = %.3f" % (
            refused, len(neg), refused / (len(neg) or 1)))


if __name__ == "__main__":
    main()
