# -*- coding: utf-8 -*-
"""解析生成的自检与合并规则（不联网）。"""
import json
import os
import tempfile
import unittest

from pipeline.analysis import (build_messages, check, clean, has_figure,
                               merge_into)


def item(**kw):
    base = {"id": "2019-408-01", "seq": 1, "subject": "ds", "answer": "B",
            "stem": "while 循环的时间复杂度", "options": {"A": "O(logn)",
                                                          "B": "O(sqrt n)"}}
    base.update(kw)
    return base


class CleanTest(unittest.TestCase):
    def test_strips_prefix_and_whitespace(self):
        self.assertEqual(clean("【解析】 先看循环\n上限"), "先看循环上限")
        self.assertEqual(clean("解析：答案选 B"), "答案选B")

    def test_strips_code_fence_and_quotes(self):
        self.assertEqual(clean("```\n正文\n```"), "正文")
        self.assertEqual(clean("“正文”"), "正文")


class CheckTest(unittest.TestCase):
    def test_accepts_normal_analysis(self):
        self.assertEqual(check("循环次数与 n 的平方根同阶，所以时间复杂度为 O(sqrt n)。"), [])

    def test_rejects_short(self):
        self.assertTrue(check("太短了"))

    def test_rejects_hedge(self):
        text = "由于看不到图，无法确定这道题的具体答案，只能给出通用步骤。"
        self.assertTrue(any("含糊" in e for e in check(text)))


class PromptTest(unittest.TestCase):
    def test_includes_answer_and_options(self):
        msgs = build_messages(item())
        self.assertIn("正确答案：B", msgs[1]["content"])
        self.assertIn("A. O(logn)", msgs[1]["content"])

    def test_figure_rule_only_when_needed(self):
        self.assertFalse(has_figure(item()))
        self.assertNotIn("看不到图", build_messages(item())[0]["content"])
        fig = item(stem="下图所示的 AOE 网中，活动 d 的最早开始时间是")
        self.assertTrue(has_figure(fig))
        self.assertIn("看不到图", build_messages(fig)[0]["content"])


class MergeTest(unittest.TestCase):
    def test_fills_only_empty_and_keeps_original(self):
        rows = [item(id="2019-408-01", analysis=""),
                item(id="2019-408-02", analysis="原卷解析"),
                item(id="2019-408-03", analysis="")]
        recs = {"2019-408-01": {"id": "2019-408-01", "analysis": "AI 解析一",
                                "origin": "ai"}}
        d = tempfile.mkdtemp()
        draft, out = os.path.join(d, "draft.jsonl"), os.path.join(d, "out.jsonl")
        with open(draft, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        stat = merge_into(draft, recs, out)
        with open(out, encoding="utf-8") as f:
            got = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(stat, {"total": 3, "filled": 1, "kept": 1, "missing": 1})
        self.assertEqual(got[0]["analysis"], "AI 解析一")
        self.assertEqual(got[0]["analysis_origin"], "ai")
        self.assertEqual(got[1]["analysis"], "原卷解析")
        self.assertNotIn("analysis_origin", got[1])

    def test_replace_overwrites_ai_but_never_source(self):
        rows = [item(id="a", analysis="旧 AI 解析", analysis_origin="ai"),
                item(id="b", analysis="原卷解析")]
        recs = {"a": {"id": "a", "analysis": "新 AI 解析", "origin": "ai"},
                "b": {"id": "b", "analysis": "不应覆盖", "origin": "ai"}}
        d = tempfile.mkdtemp()
        draft, out = os.path.join(d, "draft.jsonl"), os.path.join(d, "out.jsonl")
        with open(draft, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        merge_into(draft, recs, out, replace=True)
        with open(out, encoding="utf-8") as f:
            got = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(got[0]["analysis"], "新 AI 解析")
        self.assertEqual(got[0]["analysis_origin"], "ai")
        self.assertEqual(got[1]["analysis"], "原卷解析")


if __name__ == "__main__":
    unittest.main()
