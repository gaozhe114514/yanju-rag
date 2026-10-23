# -*- coding: utf-8 -*-
"""给选择题补解析：模型生成，单独落盘，可复核、可重跑。

2019 卷 1-40 只转写了答案字母，没有解析；用户问「为什么」时检索层手里没有可引用的
解释。这里基于「题干 + 选项 + 正确答案」生成解析，产物写到 data/curated/{year}_analysis.jsonl，
带 origin/model 字段。它是 AI 写的，不是原卷解析：合并进 draft 后 status 仍是 draft，
进 data/out 之前必须人工校对。

用法：
  py -m pipeline.analysis --draft data/work/2019_draft.jsonl \
      --out data/curated/2019_analysis.jsonl --apply-to data/work/2019_draft.jsonl
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List

from .normalize import has_unmapped

SUBJECT_NAME = {"ds": "数据结构", "co": "计算机组成原理",
                "os": "操作系统", "cn": "计算机网络"}
FIG_REF = re.compile(r"如图|图所示|下图|上图|图中|题\s*\d+\s*图")

SYSTEM = """你是 408 考研助教。根据给定的题干、选项和正确答案，写一段简洁的中文解析。

要求：
1. 先说明正确答案为什么成立，必要时指出关键干扰项错在哪。
2. 只使用题干、选项和正确答案能直接支持的知识；不确定的细节不要编造。
3. 公式、符号、选项内容保持原样，不要复述整道题。
4. 只输出解析正文，字数 60~180 字，不要「解析：」前缀，不要 Markdown，不要换行、不要分点。
5. 写之前先自己验算一遍：引用的序列、数值、选项字母必须与题干、选项完全一致；
   推理链不自洽就不要输出。"""

FIG_RULE = """6. 题干引用了图，但你看不到图：只写这类题的通用解题步骤和判断依据，
   不要给出任何具体数值结论，也不要猜测图中数据。"""

PREFIX = re.compile(r"^\s*(?:【解析】|解析\s*[:：])\s*")
HEDGE = ("无法确定", "无法判断", "无法给出", "抱歉", "不确定", "不能确定")
MIN_CHARS, MAX_CHARS = 30, 400


def _subject(item: Dict[str, Any]) -> str:
    return SUBJECT_NAME.get(item.get("subject") or "", "408")


def has_figure(item: Dict[str, Any]) -> bool:
    return bool(FIG_REF.search(item.get("stem") or ""))


def build_messages(item: Dict[str, Any]) -> List[Dict[str, str]]:
    opts = item.get("options") or {}
    lines = ["%s. %s" % (k, opts[k]) for k in "ABCD" if opts.get(k)]
    system = SYSTEM + ("\n" + FIG_RULE if has_figure(item) else "")
    user = "第 %s 题（%s）\n题干：%s\n选项：\n%s\n正确答案：%s" % (
        item.get("seq"), _subject(item), (item.get("stem") or "").strip(),
        "\n".join(lines), item.get("answer") or "")
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def clean(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = "\n".join(s.splitlines()[1:])
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = PREFIX.sub("", s.strip())
    s = s.strip().strip('"“”').strip()
    return re.sub(r"\s+", "", s)


def check(text: str) -> List[str]:
    """生成结果的自检；返回空列表才允许落盘。"""
    errs = []
    if len(text) < MIN_CHARS:
        errs.append("解析过短（%d 字）" % len(text))
    if len(text) > MAX_CHARS:
        errs.append("解析过长（%d 字）" % len(text))
    if any(w in text for w in HEDGE):
        errs.append("解析含含糊表述，疑似没做出来")
    if has_unmapped(text):
        errs.append("解析含未映射的 PDF 私有区字符")
    if "解析" in text[:4]:
        errs.append("解析带了前缀")
    return errs


class DeepSeekClient:
    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base: str = "https://api.deepseek.com/chat/completions",
                 timeout: int = 90):
        self.api_key, self.model, self.base = api_key, model, base
        self.timeout = timeout

    def chat(self, messages: List[Dict[str, str]]) -> str:
        body = {"model": self.model, "temperature": 0.3, "max_tokens": 500,
                "messages": messages}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        })
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return payload["choices"][0]["message"]["content"]
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("调用失败：%s" % last)


def load_analysis(path: str) -> Dict[str, Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                out[rec["id"]] = rec
    return out


def write_analysis(path: str, records: Dict[str, Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def merge_into(draft_path: str, records: Dict[str, Dict[str, Any]],
               out_path: str, replace: bool = False) -> Dict[str, int]:
    """只填空解析，原卷解析永远优先；status 不升级，等人工校对。

    replace=True 时用产物覆盖此前 AI 生成的解析（analysis_origin=ai），用于返工。
    """
    rows, filled, kept = [], 0, 0
    with open(draft_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)
            old = (q.get("analysis") or "").strip()
            if old and not (replace
                            and q.get("analysis_origin") in ("ai", "manual")
                            and q.get("id") in records):
                kept += 1
            else:
                rec = records.get(q.get("id"))
                if rec:
                    q["analysis"] = rec["analysis"]
                    q["analysis_origin"] = rec.get("origin", "ai")
                    filled += 1
            rows.append(q)
    with open(out_path, "w", encoding="utf-8") as f:
        for q in rows:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    return {"total": len(rows), "filled": filled, "kept": kept,
            "missing": len(rows) - filled - kept}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True)
    ap.add_argument("--out", required=True, help="AI 解析落盘路径（JSONL）")
    ap.add_argument("--provider", default="deepseek", choices=["deepseek", "mock"])
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="只生成前 N 条，用于冒烟")
    ap.add_argument("--redo", action="store_true", help="忽略已有产物重新生成")
    ap.add_argument("--ids", help="只返工这些题号，逗号分隔，如 2019-408-05,2019-408-11")
    ap.add_argument("--apply-to", help="合并结果写到的 draft 路径（一般就是原 draft）")
    ap.add_argument("--replace", action="store_true",
                    help="合并时覆盖已有 AI 解析（原卷解析不动）")
    args = ap.parse_args()

    items = []
    with open(args.draft, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    todo = [q for q in items if not (q.get("analysis") or "").strip()]
    if args.limit:
        todo = todo[:args.limit]

    records = {} if args.redo else load_analysis(args.out)
    if args.ids:
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        pending = [q for q in items if q["id"] in want]
    else:
        pending = [q for q in todo if q["id"] not in records]
    failed: List = []

    if not pending:
        results = {}
        print("没有待生成的题，直接合并已有产物")
    elif args.provider == "mock":
        def gen(item):
            return "【mock】%s 的正确答案是 %s，解析还没接入真实模型。" % (
                item["id"], item.get("answer"))
        results = {q["id"]: gen(q) for q in pending}
    else:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise SystemExit("缺少环境变量 DEEPSEEK_API_KEY；或改用 --provider mock")
        client = DeepSeekClient(key, args.model)

        def run(item):
            return item, clean(client.chat(build_messages(item)))

        results = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run, q): q for q in pending}
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    item, text = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed.append((q["id"], str(e)))
                    continue
                errs = check(text)
                if errs:
                    failed.append((q["id"], "; ".join(errs)))
                    continue
                results[q["id"]] = text
                print("  ok %s %d 字" % (q["id"], len(text)))
        for qid, why in failed:
            print("  ! %s %s" % (qid, why))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for q in items:
        if q["id"] in results:
            records[q["id"]] = {
                "id": q["id"], "seq": q.get("seq"),
                "analysis": results[q["id"]], "origin": "ai",
                "model": args.model if args.provider == "deepseek" else "mock",
                "generated_at": now,
            }
    # 按 draft 顺序输出，便于 diff
    order = {q["id"]: i for i, q in enumerate(items)}
    records = dict(sorted(records.items(), key=lambda kv: order.get(kv[0], 10 ** 6)))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    write_analysis(args.out, records)
    print("解析落盘：%s（%d 条，新增 %d）" % (args.out, len(records), len(results)))

    if args.apply_to:
        stat = merge_into(args.draft, records, args.apply_to,
                          replace=args.replace)
        print("合并到 %s：共 %d 题，补解析 %d，保留原解析 %d，仍缺 %d" % (
            args.apply_to, stat["total"], stat["filled"], stat["kept"],
            stat["missing"]))
    if failed:
        print("失败 %d 条，重跑同一条命令会只补缺的部分" % len(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
