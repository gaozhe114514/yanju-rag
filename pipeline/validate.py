# -*- coding: utf-8 -*-
"""题库校验。目标：把'机器抽错了'在进检索库之前全部拦下来。"""
import re
from typing import Dict, List, Tuple

SUBJECTS = {"ds", "co", "os", "cn"}


def check_one(item: Dict) -> List[Tuple[str, str]]:
    errs: List[Tuple[str, str]] = []
    seq, t = item.get("seq"), item.get("type")
    stem = (item.get("stem") or "").strip()

    if not isinstance(seq, int) or not (1 <= seq <= 47):
        errs.append(("E", "题号非法: %r" % (seq,)))
    if t not in ("single", "comprehensive"):
        errs.append(("E", "type 非法: %r" % (t,)))
    if seq and t and ((seq <= 40) != (t == "single")):
        errs.append(("E", "题号与题型不匹配: seq=%s type=%s" % (seq, t)))
    if len(stem) < 8:
        errs.append(("E", "题干过短或为空"))
    if stem.count("$") % 2 != 0:
        errs.append(("E", "LaTeX $ 未配对，渲染会炸"))

    if t == "single":
        opts = item.get("options") or {}
        for k in "ABCD":
            if not (opts.get(k) or "").strip():
                errs.append(("E", "选项 %s 缺失" % k))
        if item.get("answer") not in ("A", "B", "C", "D"):
            errs.append(("E", "答案非法: %r" % (item.get("answer"),)))
    else:
        if item.get("options"):
            errs.append(("W", "综合题不应有 options"))
        if not (item.get("analysis") or "").strip():
            errs.append(("E", "综合题缺参考答案/解析"))

    if not item.get("subject"):
        errs.append(("W", "subject 未填"))
    elif item["subject"] not in SUBJECTS:
        errs.append(("E", "subject 非法: %r" % (item["subject"],)))
    if not isinstance(item.get("score"), int):
        errs.append(("W", "score 未填"))
    if not item.get("kp_ids"):
        errs.append(("W", "kp_ids 为空（打标阶段补）"))
    return errs


def check_paper(items: List[Dict]) -> Tuple[List[Tuple[str, str]], Dict]:
    errs: List[Tuple[str, str]] = []
    seqs = [it.get("seq") for it in items]
    if len(set(seqs)) != len(seqs):
        errs.append(("E", "存在重复题号"))
    if seqs and sorted(seqs) != list(range(1, max(seqs) + 1)):
        missing = sorted(set(range(1, max(seqs) + 1)) - set(seqs))
        lvl = "E" if len(items) >= 47 else "W"
        errs.append((lvl, "题号不连续（缺 %d 题：%s%s）" % (
            len(missing), missing[:12], " ..." if len(missing) > 12 else "")))
    total = sum(it.get("score") or 0 for it in items)
    stat = {"count": len(items), "total_score": total}
    if len(items) == 47 and total != 150:
        errs.append(("E", "分值合计 %s ≠ 150" % total))
    elif len(items) != 47:
        errs.append(("W", "仅 %d 题，未做 150 分合计校验（部分录入属正常）" % len(items)))
    return errs, stat


def report(items: List[Dict]) -> Tuple[int, str, Dict]:
    lines, n_err = [], 0
    stat: Dict = {}
    for it in items:
        for lvl, msg in check_one(it):
            lines.append("[%s] #%s %s" % (lvl, it.get("seq"), msg))
            n_err += lvl == "E"
    perrs, stat = check_paper(items)
    for lvl, msg in perrs:
        lines.append("[%s] 整卷 %s" % (lvl, msg))
        n_err += lvl == "E"
    head = "校验：%d 题 / %d 分 / %d 个错误 / %d 条提示" % (
        stat.get("count", 0), stat.get("total_score", 0), n_err, len(lines))
    return n_err, head + "\n" + "\n".join(lines), stat
