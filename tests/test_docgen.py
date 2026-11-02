# -*- coding: utf-8 -*-
"""创作页大纲与导出的单元测试：不调真模型，不碰数据库。"""
import io
import json
import unittest

from answer import docgen


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if not self.replies:
            raise docgen.ProviderError("没有更多回复")
        return self.replies.pop(0)


def sample_outline(kind="doc"):
    return {
        "kind": kind,
        "title": "进程调度",
        "subtitle": "从状态到时间片",
        "sections": [
            {"heading": "进程状态", "bullets": ["就绪", "运行", "阻塞"],
             "notes": "先画状态图"},
            {"heading": "时间片轮转", "bullets": ["按队列轮转", "用完让出 CPU"],
             "notes": ""},
        ],
    }


class NormalizeTest(unittest.TestCase):
    def test_accepts_valid_and_defaults_kind(self):
        out = docgen.normalize_outline(sample_outline())
        self.assertEqual(out["kind"], "doc")
        self.assertEqual(out["title"], "进程调度")
        self.assertEqual(len(out["sections"]), 2)

    def test_rejects_too_few_sections(self):
        with self.assertRaises(docgen.CreateError):
            docgen.normalize_outline({"title": "x", "sections": [
                {"heading": "只有一节", "bullets": ["a"]}]})

    def test_drops_empty_sections_and_dedupes_bullets(self):
        out = docgen.normalize_outline({
            "title": "T", "sections": [
                {"heading": "A", "bullets": ["x", "x", ""]},
                {"heading": "", "bullets": ["y"]},
                {"heading": "B", "bullets": ["z"]},
            ]})
        self.assertEqual([s["heading"] for s in out["sections"]], ["A", "B"])
        self.assertEqual(out["sections"][0]["bullets"], ["x"])

    def test_clamps_overlong_text(self):
        out = docgen.normalize_outline({
            "title": "题" * 200,
            "sections": [{"heading": "H" * 100, "bullets": ["B" * 500]},
                         {"heading": "I", "bullets": ["y"]}],
        })
        self.assertEqual(len(out["title"]), docgen.MAX_TITLE)
        self.assertEqual(len(out["sections"][0]["heading"]), docgen.MAX_HEADING)
        self.assertEqual(len(out["sections"][0]["bullets"][0]), docgen.MAX_BULLET)


class DemoTest(unittest.TestCase):
    def test_demo_uses_notes_and_is_marked(self):
        out = docgen.demo_outline("进程调度", "slides", 5,
                                  "进程状态\n时间片轮转\n上下文切换")
        self.assertEqual(len(out["sections"]), 5)
        text = json.dumps(out, ensure_ascii=False)
        self.assertIn("进程状态", text)
        self.assertIn("演示草稿", out["subtitle"])

    def test_demo_without_notes_still_has_bullets(self):
        out = docgen.demo_outline("图论", "doc", 3)
        self.assertEqual(len(out["sections"]), 3)
        for sec in out["sections"]:
            self.assertGreaterEqual(len(sec["bullets"]), 2)

    def test_generate_without_provider_returns_demo(self):
        got = docgen.generate_outline(None, "图论", "doc", 4)
        self.assertTrue(got["demo"])
        self.assertEqual(len(got["outline"]["sections"]), 4)


class GenerateTest(unittest.TestCase):
    def test_model_json_is_normalized(self):
        provider = FakeProvider([json.dumps({
            "title": "图论入门", "subtitle": "",
            "sections": [
                {"heading": "图", "bullets": ["顶点", "边"], "notes": ""},
                {"heading": "遍历", "bullets": ["DFS", "BFS"], "notes": ""},
            ]})])
        got = docgen.generate_outline(provider, "图论", "slides", 3)
        self.assertFalse(got["demo"])
        self.assertEqual(got["outline"]["kind"], "slides")
        self.assertEqual(got["outline"]["title"], "图论入门")
        self.assertEqual(provider.calls, 1)

    def test_bad_json_retries_then_succeeds(self):
        good = json.dumps({
            "title": "图论", "sections": [
                {"heading": "A", "bullets": ["a"]},
                {"heading": "B", "bullets": ["b"]}]})
        provider = FakeProvider(["不是 JSON", good])
        got = docgen.generate_outline(provider, "图论", "doc", 2)
        self.assertEqual(got["outline"]["title"], "图论")
        self.assertEqual(provider.calls, 2)

    def test_two_bad_replies_raise(self):
        provider = FakeProvider(["bad", "still bad"])
        with self.assertRaises(docgen.CreateError):
            docgen.generate_outline(provider, "图论", "doc", 2)


class ExportTest(unittest.TestCase):
    def test_markdown_contains_title_and_bullets(self):
        text = docgen.render_markdown(sample_outline())
        self.assertIn("# 进程调度", text)
        self.assertIn("- 按队列轮转", text)
        self.assertIn("AI 草稿", text)

    def test_docx_roundtrip(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx 未安装")
        data = docgen.render_docx(sample_outline())
        self.assertTrue(data.startswith(b"PK"))
        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("进程调度", text)
        self.assertIn("时间片轮转", text)
        self.assertIn("讲者提示：先画状态图", text)

    def test_pptx_roundtrip(self):
        try:
            from pptx import Presentation
        except ImportError:
            self.skipTest("python-pptx 未安装")
        data = docgen.render_pptx(sample_outline("slides"))
        self.assertTrue(data.startswith(b"PK"))
        prs = Presentation(io.BytesIO(data))
        self.assertGreaterEqual(len(prs.slides), 4)
        all_text = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    all_text.append(shape.text_frame.text)
        joined = "\n".join(all_text)
        self.assertIn("进程调度", joined)
        self.assertIn("时间片轮转", joined)
        self.assertIn("谢谢", joined)

    def test_filename_strips_path_and_illegal_chars(self):
        name = docgen.safe_filename('  a/b:c*?"<>|  ', "docx")
        self.assertEqual(name, "a b c.docx")
        self.assertEqual(docgen.safe_filename("...", "pptx"), "言据创作.pptx")


if __name__ == "__main__":
    unittest.main()
