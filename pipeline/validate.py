# -*- coding: utf-8 -*-
"""题库校验。目标：把'机器抽错了'在进检索库之前全部拦下来。

校验器自己不能崩：模型返回缺字段、错类型是常态，这里要把它们变成错误项，而不是异常。
"""
from typing import Dict, List, Tuple

from .normalize import has_unmapped

SUBJECTS = {"ds", "co", "os", "cn"}


def check_one(item: Dict) -> List[Tuple[str, str]]:
    errs: List[Tuple[str, str]] = []
    seq, t = item.get("seq"), item.get("type")

    stem = item.get("stem")
    if not isinstance(stem, str):
        errs.append(("E", "stem 不是字符串: %s" % type(stem).__name__))
        stem = ""
    stem = stem.strip()

    if not isinstance(seq, int) or isinstance(seq, bool) or not (1 <= seq <= 47):
        errs.append(("E", "题号非法: %r" % (seq,)))
    if t not in ("single", "comprehensive"):
        errs.append(("E", "type 非法: %r" % (t,)))
    if isinstance(seq, int) and t in ("single", "comprehensive") and ((seq <= 40) != (t == "single")):
        errs.append(("E", "题号与题型不匹配: seq=%s type=%s" % (seq, t)))
    if len(stem) < 8:
        errs.append(("E", "题干过短或为空"))
    if stem.count("$") % 2 != 0:
        errs.append(("E", "LaTeX $ 未配对，渲染会炸"))

    opts = item.get("options")
    if opts is None:
        opts = {}
    if not isinstance(opts, dict):
        errs.append(("E", "options 不是对象: %s" % type(opts).__name__))
        opts = {}

    if t == "single":
        for k in "ABCD":
            if not isinstance(opts.get(k), str) or not opts[k].strip():
                errs.append(("E", "选项 %s 缺失" % k))
        if item.get("answer") not in ("A", "B", "C", "D"):
            errs.append(("E", "答案非法: %r" % (item.get("answer"),)))
    elif t == "comprehensive":
        if opts:
            errs.append(("W", "综合题不应有 options"))
        analysis = item.get("analysis")
        if not isinstance(analysis, str) or not analysis.strip():
            errs.append(("E", "综合题缺参考答案/解析"))

    subject = item.get("subject")
    if not subject:
        errs.append(("W", "subject 未填"))
    elif not isinstance(subject, str) or subject not in SUBJECTS:
        errs.append(("E", "subject 非法: %r" % (subject,)))

    if not isinstance(item.get("score"), int) or isinstance(item.get("score"), bool):
        errs.append(("W", "score 未填或非整数"))

    kps = item.get("kp_ids")
    if not isinstance(kps, list):
        errs.append(("E", "kp_ids 不是数组: %s" % type(kps).__name__))
    elif not kps:
        errs.append(("W", "kp_ids 为空（打标阶段补）"))

    # PDF 抽取残留：未映射的私有区字符会污染原文和引用校验
    fields = [("stem", stem), ("analysis", item.get("analysis")),
              ("answer", item.get("answer"))]
    fields += [("选项 %s" % k, v) for k, v in opts.items() if isinstance(v, str)]
    for name, val in fields:
        if isinstance(val, str) and has_unmapped(val):
            errs.append(("E", "%s 含未映射的 PDF 私有区字符" % name))
    return errs


def check_paper(items: List[Dict]) -> Tuple[List[Tuple[str, str]], Dict]:
    errs: List[Tuple[str, str]] = []
    seqs = [it.get("seq") for it in items]
    valid = [s for s in seqs if isinstance(s, int) and not isinstance(s, bool)]

    if len(valid) != len(seqs):
        errs.append(("E", "存在非法题号，已跳过连续性校验"))
    if len(set(valid)) != len(valid):
        errs.append(("E", "存在重复题号"))
    if valid != sorted(valid):
        errs.append(("E", "题号未按升序排列"))
    if valid:
        lo, hi = 1, max(valid)
        missing = sorted(set(range(lo, hi + 1)) - set(valid))
        if missing:
            lvl = "E" if len(items) >= 47 else "W"
            errs.append((lvl, "题号不连续（缺 %d 题：%s%s）" % (
                len(missing), missing[:12], " ..." if len(missing) > 12 else "")))

    total = sum(it.get("score") or 0 for it in items if isinstance(it.get("score"), int))
    stat = {"count": len(items), "total_score": total}
    if len(items) == 47 and total != 150:
        errs.append(("E", "分值合计 %s ≠ 150" % total))
    elif len(items) != 47:
        errs.append(("W", "仅 %d 题，未做 150 分合计校验（部分录入属正常）" % len(items)))
    return errs, stat


def report(items: List[Dict]) -> Tuple[int, str, Dict]:
    lines, n_err, n_warn = [], 0, 0
    for it in items:
        for lvl, msg in check_one(it):
            lines.append("[%s] #%s %s" % (lvl, it.get("seq"), msg))
            n_err += lvl == "E"
            n_warn += lvl == "W"
    perrs, stat = check_paper(items)
    for lvl, msg in perrs:
        lines.append("[%s] 整卷 %s" % (lvl, msg))
        n_err += lvl == "E"
        n_warn += lvl == "W"
    stat["errors"], stat["warnings"] = n_err, n_warn
    head = "校验：%d 题 / %d 分 / %d 个错误 / %d 条提示" % (
        stat.get("count", 0), stat.get("total_score", 0), n_err, n_warn)
    return n_err, head + "\n" + "\n".join(lines), stat
