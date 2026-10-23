# -*- coding: utf-8 -*-
"""端到端问答：py -m answer.cli "问题" --provider mock --mode hybrid"""
import argparse
import glob
import sys

from retrieval.chunk import load_chunks
from retrieval.search import MODES, build_searcher

from .gate import RefusalGate
from .pipeline import AnswerPipeline
from .providers import DeepSeekProvider, ExtractiveProvider, ProviderError


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--input", nargs="+",
                    default=["data/work/2019_draft.jsonl"])
    ap.add_argument("--mode", default="hybrid", choices=list(MODES))
    ap.add_argument("--provider", default="mock", choices=["mock", "deepseek"])
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--min-margin", type=float, default=0.0)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--no-retry", action="store_true")
    args = ap.parse_args()

    paths = []
    for p in args.input:
        paths.extend(sorted(glob.glob(p)) or [p])
    searcher = build_searcher(load_chunks(paths), mode=args.mode,
                              model_name=args.model,
                              reranker_model=args.reranker_model,
                              k_final=args.top_k)
    if args.provider == "deepseek":
        try:
            provider = DeepSeekProvider()
        except ProviderError as e:
            print("provider 初始化失败：%s" % e)
            sys.exit(2)
    else:
        provider = ExtractiveProvider()

    pipeline = AnswerPipeline(
        searcher, provider,
        gate=RefusalGate(args.min_score, args.min_margin),
        retry=not args.no_retry)
    res = pipeline.answer(args.query)

    print("query: %s  (%s / %s, 尝试 %d 次)" % (
        args.query, args.mode, args.provider, res.attempts))
    if res.refused:
        print("拒答：%s" % res.reason)
    else:
        print("回答：%s" % res.answer)
        for c in res.citations:
            print("引用 [%s] %s" % (c.get("chunk_id"), c.get("quote")))
    if res.hits:
        print("召回：")
        for h in res.hits:
            print("  %s  score=%.3f" % (h.chunk.id, h.score))


if __name__ == "__main__":
    main()
