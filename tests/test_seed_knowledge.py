# -*- coding: utf-8 -*-
"""408 课程笔记入库脚本的纯函数测试：清洗、切分、标题、去重键。"""
import tempfile
import unittest
from pathlib import Path

from scripts.seed_408_knowledge import (
    build_documents, clean_markdown, first_heading, normalize, sha256_text,
    split_document)


class CleanTest(unittest.TestCase):
    def test_images_are_removed_but_text_stays(self):
        raw = ("# 第一章 概述\r\n\r\n"
               "![图](https://cdn.example.com/a.png)\r\n\r\n"
               "正文第一句。\r\n"
               "<img src=\"https://example.com/b.png\">\r\n"
               "正文第二句。")
        text = clean_markdown(raw)
        self.assertNotIn("![]", text)
        self.assertNotIn("cdn.example.com", text)
        self.assertNotIn("<img", text)
        self.assertIn("正文第一句。", text)
        self.assertIn("正文第二句。", text)
        self.assertNotIn("\r", text)

    def test_blank_line_runs_collapse(self):
        self.assertEqual(clean_markdown("甲\n\n\n\n乙"), "甲\n\n乙")

    def test_first_heading(self):
        self.assertEqual(first_heading("# 第三章 网络层\n正文"),
                         "第三章 网络层")
        self.assertEqual(first_heading("正文没有标题"), "")

    def test_sha_is_stable_across_newlines(self):
        self.assertEqual(sha256_text("甲\r\n乙"), sha256_text("甲\n乙"))
        self.assertEqual(sha256_text(normalize("甲\n乙")), sha256_text("甲\n乙"))


class SplitTest(unittest.TestCase):
    def test_short_text_single_part(self):
        self.assertEqual(split_document("只有一段。"), ["只有一段。"])
        self.assertEqual(split_document(""), [])

    def test_parts_are_within_limit_and_complete(self):
        paras = ["第%d段：%s" % (i, "内容" * 50) for i in range(1, 61)]
        text = "\n\n".join(paras)
        parts = split_document(text, limit=400)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 400 for p in parts), [len(p) for p in parts])
        joined = "\n\n".join(parts)
        for i in range(1, 61):
            self.assertIn("第%d段：" % i, joined)

    def test_oversized_paragraph_is_hard_split(self):
        text = "无换行" * 300
        parts = split_document(text, limit=200)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p) <= 200 for p in parts))


class DocumentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "DataStructure").mkdir()
        (self.base / "DataStructure" / "1-summary.md").write_text(
            "# 第一章 数据结构概述\n\n" + "数据结构正文。" * 10,
            encoding="utf-8")
        (self.base / "ComputerNetwork").mkdir()
        (self.base / "ComputerNetwork" / "0-summary.md").write_text(
            "# 第一章 体系结构\n\n" + "网络正文。" * 10, encoding="utf-8")
        (self.base / "LICENSE.txt").write_text(
            "MIT License\n\nCopyright (c) 2025", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_titles_are_prefixed_by_subject(self):
        docs = build_documents(self.base)
        titles = [d["title"] for d in docs]
        self.assertIn("数据结构 · 第一章 数据结构概述", titles)
        self.assertIn("计算机网络 · 第一章 体系结构", titles)
        self.assertTrue(all(len(t) <= 80 for t in titles))

    def test_license_becomes_a_document(self):
        docs = build_documents(self.base)
        lic = [d for d in docs if d["title"].startswith("来源与许可")]
        self.assertEqual(len(lic), 1)
        self.assertIn("MIT License", lic[0]["text"])

    def test_source_keeps_repo_path(self):
        docs = build_documents(self.base)
        ds = [d for d in docs if d["subject"] == "数据结构"][0]
        self.assertIn("CS408_Note", ds["source"])
        self.assertIn("1-summary.md", ds["source"])


if __name__ == "__main__":
    unittest.main()
