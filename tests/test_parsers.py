# -*- coding: utf-8 -*-
"""本地文件解析：能取到的文字要取全，取不到的要说明原因，不许假装解析成功。"""
import io
import os
import unittest
import zipfile
from unittest import mock

from kb import parsers
from kb.parsers import ParseError, extract_text

LONG = "".join(
    "第%d段：进程是资源分配的单位，线程是调度的基本单位，二者共享同一段地址空间。\n" % i
    for i in range(1, 12)
)


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def make_docx(*paras):
    body = "".join(
        '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % p
        for p in paras)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>%s</w:body></w:document>" % body)
    return make_zip({"word/document.xml": xml})


def make_pptx(*slides):
    entries = {}
    for i, lines in enumerate(slides, 1):
        body = "".join("<a:p><a:r><a:t>%s</a:t></a:r></a:p>" % line
                       for line in lines)
        entries["ppt/slides/slide%d.xml" % i] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody>%s</p:txBody></p:sp>"
            "</p:spTree></p:cSld></p:sld>" % body)
    return make_zip(entries)


class TextLikeTest(unittest.TestCase):
    def test_plain_text_utf8(self):
        text, meta = extract_text("笔记.md", LONG.encode("utf-8"))
        self.assertIn("进程是资源分配的单位", text)
        self.assertEqual(meta["ext"], ".md")

    def test_gbk_text_does_not_mojibake(self):
        text, _ = extract_text("老讲义.txt", LONG.encode("gb18030"))
        self.assertIn("进程是资源分配的单位", text)

    def test_html_drops_script_and_tags(self):
        html = ("<html><body><h1>操作系统</h1><p>%s</p>"
                "<script>alert('x')</script></body></html>" % LONG)
        text, _ = extract_text("page.html", html.encode("utf-8"))
        self.assertIn("操作系统", text)
        self.assertIn("进程是资源分配的单位", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("<h1>", text)

    def test_csv_and_json_are_read_as_text(self):
        data = '{"note": "%s"}' % LONG.replace("\n", " ")
        text, _ = extract_text("a.json", data.encode("utf-8"))
        self.assertIn("进程是资源分配的单位", text)


class OfficeTest(unittest.TestCase):
    def test_docx_keeps_paragraph_breaks(self):
        data = make_docx("第一段：进程是资源分配的单位。", "第二段：线程是调度的基本单位。",
                         "第三段：两者共享地址空间，切换开销不同。")
        text, _ = extract_text("讲义.docx", data)
        self.assertIn("第一段", text)
        self.assertIn("\n", text)
        self.assertGreaterEqual(text.count("\n"), 1)

    def test_pptx_numbers_pages_in_order(self):
        data = make_pptx(["第一页：进程是资源分配的单位。"],
                         ["第二页：线程是调度的基本单位。"])
        text, _ = extract_text("课件.pptx", data)
        self.assertIn("第 1 页", text)
        self.assertIn("第 2 页", text)
        self.assertLess(text.index("第 1 页"), text.index("第 2 页"))

    def test_broken_docx_reports_why(self):
        with self.assertRaises(ParseError) as ctx:
            extract_text("bad.docx", b"not a zip")
        self.assertIn("打不开", str(ctx.exception))

    def test_docx_without_text_is_rejected(self):
        data = make_zip({"word/document.xml":
                         '<w:document xmlns:w="x"><w:body></w:body></w:document>'})
        with self.assertRaises(ParseError) as ctx:
            extract_text("empty.docx", data)
        self.assertIn("没有可提取的文字", str(ctx.exception))


def make_png(width=64, height=32):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


class FakeOCR:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        return self.rows, [0.01]


class OcrTest(unittest.TestCase):
    def test_image_is_read_by_ocr(self):
        fake = FakeOCR([
            [[[0, 0], [10, 0], [10, 10], [0, 10]], "第一章：操作系统概述，进程是资源分配的单位。", 0.98],
            [[[0, 20], [10, 20], [10, 30], [0, 30]], "第二章：线程是调度的基本单位。", 0.91],
        ])
        with mock.patch.object(parsers, "_ocr_engine", return_value=fake):
            text, meta = extract_text("扫描件.png", make_png())
        self.assertIn("进程是资源分配的单位", text)
        self.assertIn("线程是调度的基本单位", text)
        self.assertTrue(meta["ocr"])
        self.assertEqual(fake.calls, 1)

    def test_low_confidence_lines_are_dropped(self):
        fake = FakeOCR([
            [[[0, 0], [10, 0], [10, 10], [0, 10]], "这一行是清楚的：进程是资源分配的单位，线程是调度的基本单位。", 0.95],
            [[[0, 20], [10, 20], [10, 30], [0, 30]], "模糊噪声lll", 0.12],
        ])
        with mock.patch.object(parsers, "_ocr_engine", return_value=fake):
            text, _ = extract_text("a.jpg", make_png())
        self.assertIn("进程是资源分配的单位", text)
        self.assertNotIn("模糊噪声", text)

    def test_no_text_in_image_says_so(self):
        with mock.patch.object(parsers, "_ocr_engine", return_value=FakeOCR([])):
            with self.assertRaises(ParseError) as ctx:
                extract_text("blank.png", make_png())
        self.assertIn("没识别出文字", str(ctx.exception))

    def test_ocr_disabled_explains(self):
        with mock.patch.dict(os.environ, {"KB_OCR": "0"}):
            with mock.patch.object(parsers, "_OCR_ENGINE", None):
                with self.assertRaises(ParseError) as ctx:
                    extract_text("a.png", make_png())
        self.assertIn("KB_OCR", str(ctx.exception))

    def test_corrupt_image_reports(self):
        with mock.patch.object(parsers, "_ocr_engine", return_value=FakeOCR([])):
            with self.assertRaises(ParseError) as ctx:
                extract_text("bad.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        self.assertIn("打不开", str(ctx.exception))


class RejectTest(unittest.TestCase):
    def test_ocr_engine_missing_is_explained(self):
        with mock.patch.object(parsers, "_OCR_ENGINE", None):
            with mock.patch.dict("sys.modules", {"rapidocr_onnxruntime": None}):
                with self.assertRaises(ParseError) as ctx:
                    extract_text("scan.png", make_png())
        self.assertIn("rapidocr", str(ctx.exception))

    def test_legacy_office_is_refused(self):
        with self.assertRaises(ParseError) as ctx:
            extract_text("old.doc", b"\xd0\xcf\x11\xe0" + b"0" * 100)
        self.assertIn(".docx", str(ctx.exception))

    def test_unknown_extension_is_refused(self):
        with self.assertRaises(ParseError):
            extract_text("tool.exe", b"MZ" + b"0" * 100)

    def test_empty_and_short_files_are_refused(self):
        with self.assertRaises(ParseError):
            extract_text("a.txt", b"")
        with self.assertRaises(ParseError):
            extract_text("b.txt", "太短了".encode("utf-8"))

    def test_too_long_is_refused_instead_of_truncated(self):
        with self.assertRaises(ParseError) as ctx:
            extract_text("big.txt", ("字" * 50001).encode("utf-8"))
        self.assertIn("拆分", str(ctx.exception))

    def test_corrupt_pdf_reports_why(self):
        with self.assertRaises(ParseError):
            extract_text("bad.pdf", b"%PDF-1.4 broken")


if __name__ == "__main__":
    unittest.main()
