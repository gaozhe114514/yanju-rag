# -*- coding: utf-8 -*-
"""管线单元测试。只依赖标准库：py -m unittest discover -s tests -t . -v"""
import json
import os
import tempfile
import unittest

from pipeline.build import build_items
from pipeline.extract import mock_extract
from pipeline.review import apply_patch
from pipeline.split import Block, parse_answer_table, split_answers, split_numbered
from pipeline.validate import check_one, check_paper, report


def ok_single(seq=1):
    return {
        "seq": seq,
        "type": "single",
        "subject": "ds",
        "score": 2,
        "stem": "下列关于二叉排序树的说法中，正确的是（ ）",
        "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
        "answer": "A",
        "analysis": "解析内容",
        "kp_ids": [],
        "figures": [],
    }


class SplitTest(unittest.TestCase):
    def test_basic_blocks_and_section_boundary(self):
        text = (
            "一、单项选择题\n"
            "1. 第一题题干\nA. 甲\nB. 乙\nC. 丙\nD. 丁\n"
            "2. 第二题题干\nA. a\nB. b\nC. c\nD. d\n"
            "三、综合应用题\n"
            "41. （本题 10 分）综合题题干"
        )
        blocks, _ = split_numbered(text)
        self.assertEqual([b.seq for b in blocks], [1, 2, 41])
        self.assertNotIn("三、综合应用题", blocks[1].text)
        self.assertTrue(blocks[2].text.startswith("（本题 10 分）"))

    def test_jump_warns(self):
        blocks, warns = split_numbered("1. 甲\n5. 乙")
        self.assertEqual([b.seq for b in blocks], [1, 5])
        self.assertTrue(any("题号跳跃" in w for w in warns))

    def test_answer_marker_ignores_numbered_steps(self):
        text = (
            "1.【答案】A\n【解析】第一题\n3. 首先做某事\n4. 其次做某事\n"
            "2.【答案】B\n【解析】第二题"
        )
        ans, warns = split_answers(text)
        self.assertEqual(sorted(ans), [1, 2])
        self.assertIn("3. 首先", ans[1])
        self.assertFalse(any("题号跳跃" in w for w in warns))

    def test_answers_without_marker_falls_back(self):
        ans, warns = split_answers("1. B\n2. C")
        self.assertEqual(ans.get(1), "B")
        self.assertEqual(ans.get(2), "C")
        self.assertTrue(any("未带【答案】标记" in w for w in warns))

    def test_answer_table(self):
        ans = parse_answer_table("1-5 BCADA\n6-7 DB")
        self.assertEqual(ans[1], "B")
        self.assertEqual(ans[5], "A")
        self.assertEqual(ans[7], "B")


class ExtractTest(unittest.TestCase):
    def test_mock_single(self):
        rec = mock_extract(
            1, "题干内容\nA. 甲\nB. 乙\nC. 丙\nD. 丁",
            "1.【答案】B\n【解析】因为所以", {})
        self.assertEqual(rec["answer"], "B")
        self.assertEqual(rec["type"], "single")
        self.assertEqual(sorted(rec["options"]), list("ABCD"))
        self.assertEqual(rec["score"], 2)

    def test_mock_comprehensive(self):
        rec = mock_extract(
            41, "（本题 13 分）请回答以下问题",
            "41.【答案】见解析\n【解析】参考答案", {})
        self.assertEqual(rec["type"], "comprehensive")
        self.assertEqual(rec["options"], {})
        self.assertEqual(rec["answer"], "")
        self.assertEqual(rec["score"], 13)


class FakeProvider:
    """故意返回错题号、错题型的模型，用来验证 build 不再信任模型。"""

    def extract(self, seq, qtext, atext, meta):
        return {
            "seq": 999,
            "type": "single",
            "subject": "ds",
            "score": 2,
            "stem": "模型返回的题干内容",
            "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
            "answer": "A",
            "analysis": "",
            "kp_ids": [],
            "figures": [],
        }


class BuildTest(unittest.TestCase):
    def test_forces_seq_and_type(self):
        items = build_items([Block(seq=41, text="x", start_line=7)],
                            {}, FakeProvider(), 2024, "2019_paper.txt")
        it = items[0]
        self.assertEqual(it["seq"], 41)
        self.assertEqual(it["id"], "2024-408-41")
        self.assertEqual(it["type"], "comprehensive")
        self.assertEqual(it["options"], {})
        self.assertEqual(it["answer"], "")
        self.assertEqual(it["source_ref"]["line"], 7)
        self.assertEqual(it["status"], "draft")


class ValidateTest(unittest.TestCase):
    def test_missing_option(self):
        item = ok_single()
        del item["options"]["D"]
        errs = check_one(item)
        self.assertTrue(any("选项 D" in m for _, m in errs))

    def test_bad_types_do_not_crash(self):
        item = ok_single()
        item["stem"] = None
        item["options"] = "A. 甲"
        item["kp_ids"] = "ds"
        errs = check_one(item)
        self.assertTrue(any("stem 不是字符串" in m for _, m in errs))
        self.assertTrue(any("options 不是对象" in m for _, m in errs))
        self.assertTrue(any("kp_ids 不是数组" in m for _, m in errs))

    def test_check_paper_survives_bad_seq(self):
        bad = ok_single()
        bad["seq"] = None
        errs, _ = check_paper([ok_single(), bad])  # 不应抛 TypeError
        self.assertTrue(any("非法题号" in m for _, m in errs))

    def test_report_counts(self):
        n_err, rep, stat = report([ok_single()])
        self.assertEqual(n_err, 0)
        self.assertEqual(stat["errors"], n_err)
        self.assertGreaterEqual(stat["warnings"], 1)
        self.assertIn("校验：", rep.splitlines()[0])


class ReviewTest(unittest.TestCase):
    def test_apply_patch_overrides_fields(self):
        q = ok_single()
        q.update({"id": "2024-408-01", "year": 2024, "status": "draft"})
        with tempfile.TemporaryDirectory() as d:
            draft = os.path.join(d, "2024_draft.jsonl")
            patch = os.path.join(d, "review_patch.json")
            out = os.path.join(d, "2024.jsonl")
            with open(draft, "w", encoding="utf-8") as f:
                f.write(json.dumps(q, ensure_ascii=False) + "\n")
            with open(patch, "w", encoding="utf-8") as f:
                json.dump([{
                    "id": "2024-408-01",
                    "status": "ok",
                    "fields": {"stem": "人工改过的题干内容", "optA": "新 A",
                               "kps": "ds.tree, ds.graph"},
                }], f, ensure_ascii=False)

            stat = apply_patch(patch, draft, out)
            with open(out, encoding="utf-8") as f:
                row = json.loads(f.readline())

        self.assertEqual(stat, {"total": 1, "reviewed": 1, "flagged": 0})
        self.assertEqual(row["status"], "reviewed")
        self.assertEqual(row["stem"], "人工改过的题干内容")
        self.assertEqual(row["options"]["A"], "新 A")
        self.assertEqual(row["kp_ids"], ["ds.tree", "ds.graph"])


if __name__ == "__main__":
    unittest.main()
