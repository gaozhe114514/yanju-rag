# -*- coding: utf-8 -*-
"""数据管线总入口。

用法：
  python -m pipeline.build --paper data/raw/sample_paper.txt --answer data/raw/sample_answer.txt --year 2024
  python -m pipeline.build --year 2024 --apply-patch data/work/review_patch.json
"""
import argparse
import json
import os
from typing import Any, Dict, List

from .extract import get_provider, subject_for_seq
from .normalize import normalize_text
from .review import apply_patch, generate_html
from .split import split_answers, split_numbered
from .validate import report


def read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def build_items(blocks, ans, prov, year: int, source_file: str) -> List[Dict[str, Any]]:
    """逐题抽取。

    题号与题型以切题器为准，不信任模型返回值：模型把 13 认成 18、或返回字符串，
    会让 id 与 seq 分叉，整卷的连续性校验就失效了。
    """
    items: List[Dict[str, Any]] = []
    for b in blocks:
        rec = prov.extract(b.seq, b.text, ans.get(b.seq, ""), {"year": year})
        rec["seq"] = b.seq
        rec["type"] = "single" if b.seq <= 40 else "comprehensive"
        if rec["type"] == "comprehensive":
            rec["options"] = {}
            rec["answer"] = ""
        # 科目按题号分区定，不采用 provider 的判断：模型偶尔漏填，
        # 关键词法也会把「文件系统管理空闲磁盘块」判成数据结构。
        rec["subject"] = subject_for_seq(b.seq, year) or rec.get("subject")
        rec["id"] = "%d-408-%02d" % (year, b.seq)
        rec["year"] = year
        rec["source_ref"] = {"file": source_file, "line": b.start_line, "page": None}
        rec["status"] = "draft"
        items.append(rec)
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper")
    ap.add_argument("--answer")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--provider", default="mock")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--apply-patch")
    args = ap.parse_args()

    work = os.path.join(args.outdir, "work")
    out = os.path.join(args.outdir, "out")
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    draft_path = os.path.join(work, "%d_draft.jsonl" % args.year)

    if args.apply_patch:
        stat = apply_patch(args.apply_patch, draft_path, os.path.join(out, "%d.jsonl" % args.year))
        print("合并完成：%d 题，通过 %d，标记问题 %d" % (stat["total"], stat["reviewed"], stat["flagged"]))
        return

    if not args.paper:
        raise SystemExit("需要 --paper（或使用 --apply-patch）")

    paper = normalize_text(read(args.paper))
    blocks, w1 = split_numbered(paper)
    ans: Dict[int, str] = {}
    w2: List[str] = []
    if args.answer and os.path.exists(args.answer):
        ans, w2 = split_answers(normalize_text(read(args.answer)))

    print("切题：%d 道（题号 %s）" % (len(blocks), [b.seq for b in blocks]))
    for w in w1:
        print("  ! [试卷] " + w)
    for w in w2:
        print("  ! [答案] " + w)
    no_ans = [b.seq for b in blocks if not ans.get(b.seq)]
    if no_ans:
        print("  ! 无答案/解析的题号：%s" % no_ans)

    prov = get_provider(args.provider)
    items = build_items(blocks, ans, prov, args.year, os.path.basename(args.paper))

    with open(draft_path, "w", encoding="utf-8") as f:
        for r in items:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("草稿：%s" % draft_path)

    n_err, rep, stat = report(items)
    with open(os.path.join(work, "%d_validate.txt" % args.year), "w", encoding="utf-8") as f:
        f.write(rep + "\n")
    print(rep)

    raws = {b.seq: {"q": b.text, "a": ans.get(b.seq, "")} for b in blocks}
    hp = os.path.join(work, "%d_review.html" % args.year)
    generate_html(items, raws, hp, "%d 年 408 校对" % args.year)
    print("校对页：%s（浏览器打开，核对后导出 review_patch.json）" % hp)
    print("下一步：python -m pipeline.build --year %d --apply-patch data/work/review_patch.json" % args.year)
    print("结果：%d 个错误，%d 条提示" % (n_err, stat.get("warnings", 0)))


if __name__ == "__main__":
    main()
