# -*- coding: utf-8 -*-
import unittest

from api.catalog import (brief, detail, filter_questions, subject_counts,
                         subject_label)


ROWS = [
    {"id": "2019-408-01", "seq": 1, "year": 2019, "type": "single",
     "subject": "ds", "stem": "时间复杂度分析", "options": {"A": "O(n)"},
     "answer": "A", "analysis": "解析", "status": "draft",
     "source_ref": {"file": "p.txt", "line": 1}, "figures": []},
    {"id": "2019-408-31", "seq": 31, "year": 2019, "type": "single",
     "subject": "co", "stem": "页式存储地址转换", "options": {"A": "081H"},
     "answer": "A", "analysis": "", "status": "draft",
     "source_ref": {}, "figures": []},
    {"id": "2019-408-47", "seq": 47, "year": 2019, "type": "comprehensive",
     "subject": "", "stem": "如图所示，求最短路径", "options": {},
     "answer": "见解析", "analysis": "", "status": "draft",
     "source_ref": {}, "figures": []},
]


class LabelTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(subject_label("ds"), "数据结构")
        self.assertEqual(subject_label(""), "未分类")
        self.assertEqual(subject_label("xx"), "xx")


class FilterTest(unittest.TestCase):
    def test_filter_by_subject(self):
        total, rows = filter_questions(ROWS, subject="ds")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], "2019-408-01")

    def test_filter_unclassified(self):
        total, rows = filter_questions(ROWS, subject="none")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], "2019-408-47")

    def test_search_stem_and_option_and_label(self):
        self.assertEqual(filter_questions(ROWS, query="页式")[0], 1)
        self.assertEqual(filter_questions(ROWS, query="081H")[0], 1)
        self.assertEqual(filter_questions(ROWS, query="操作系统")[0], 0)
        self.assertEqual(filter_questions(ROWS, query="计算机组成原理")[0], 1)

    def test_pagination(self):
        total, rows = filter_questions(ROWS, limit=2, offset=1)
        self.assertEqual(total, 3)
        self.assertEqual([r["id"] for r in rows],
                         ["2019-408-31", "2019-408-47"])

    def test_year(self):
        self.assertEqual(filter_questions(ROWS, year=2020)[0], 0)


class PayloadTest(unittest.TestCase):
    def test_brief_has_options_but_no_answer(self):
        item = brief(ROWS[0])
        self.assertNotIn("answer", item)
        self.assertNotIn("analysis", item)
        self.assertEqual(item["options"], {"A": "O(n)"})
        self.assertTrue(item["has_options"])

    def test_detail_marks_missing_figure(self):
        item = detail(ROWS[2])
        self.assertTrue(item["needs_figure"])
        self.assertEqual(item["subject_label"], "未分类")
        self.assertEqual(item["answer"], "见解析")

    def test_subject_counts_order(self):
        counts = subject_counts(ROWS)
        self.assertEqual([c["key"] for c in counts], ["ds", "co", ""])
        self.assertEqual(counts[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
