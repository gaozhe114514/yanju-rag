# -*- coding: utf-8 -*-
"""结构化表格：进料时按行列抽出来，统计直接数，不再从正文格式反推。

线上那份名单没有表头，正文又是「学号 | 姓名 | 性别」这样的拼行，统计层
只能猜哪一行是表头。这里验证换了开源的解析引擎（openpyxl / python-docx /
pdfplumber）之后，行列在解析那一刻就定下来，没有表头的表也不会少算一行。
"""
import io
import unittest

from answer.aggregate import check_counts, table_stats
from kb.chunking import split_text
from kb.parsers import extract_document
from kb.tables import extract_tables
from retrieval.chunk import Chunk

MALES = ["陈畅", "陈冬阳", "高哲", "郭轩赫", "侯煜恒", "郝凌恺", "李怡宝",
         "林志翔", "陆翔宇", "毛恒臣", "任昭旭", "孙正阳", "王玉川", "闫冠南",
         "杨政道", "王知博", "张文杰", "赵维彬"]
FEMALES = ["陈曦", "贾丽苑", "翟景华", "李倩颖", "路佳怡", "石佳姿",
           "孙源晨", "王茜玮", "于孟楠", "张熙雯", "张语桐", "赵晓灿"]


def roster_rows():
    rows = []
    n = 1
    for who, sex in [(m, "男") for m in MALES] + [(f, "女") for f in FEMALES]:
        rows.append([542313331100 + n, who, sex, "软件学院", 2023])
        n += 1
    return rows


def make_xlsx(rows, sheet="Sheet1"):
    """用 openpyxl 写一份真正的 .xlsx（带 Content_Types，和用户上传的一样）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_docx_table(rows):
    from docx import Document
    doc = Document()
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            table.cell(i, j).text = str(value)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def doc_chunks(text, title="班级名单"):
    return [Chunk(id="doc-1-%03d" % p["seq"], seq=p["seq"], text=p["text"],
                  kind="doc", title=title,
                  source_ref={"document_id": "doc-1", "document_title": title,
                              "start": p["start"], "end": p["end"]})
            for p in split_text(text)]


class XlsxTableTest(unittest.TestCase):
    def test_headerless_roster_keeps_all_30_rows(self):
        tables = extract_tables("名单.xlsx", make_xlsx(roster_rows()))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["sheet"], "Sheet1")
        self.assertEqual(len(tables[0]["rows"]), 30)

    def test_header_row_is_extracted_as_a_row(self):
        rows = [["学号", "姓名", "性别"]] + roster_rows()[:3]
        tables = extract_tables("名单.xlsx", make_xlsx(rows))
        self.assertEqual(tables[0]["rows"][0], ["学号", "姓名", "性别"])

    def test_numbers_and_dates_are_written_as_text(self):
        from datetime import datetime
        tables = extract_tables(
            "成绩.xlsx",
            make_xlsx([["姓名", "分数", "日期"], ["陈畅", 95.0, datetime(2026, 3, 1)]],
                      sheet="成绩"))
        self.assertEqual(tables[0]["rows"][1], ["陈畅", "95", "2026-03-01"])

    def test_stats_count_structured_rows_not_text(self):
        # 正文只交代了「一共 30 人」，没有任何表格格式；结构化表格仍然数得准
        chunks = doc_chunks("这是一份智能科学与技术 23-01 班的名单，一共 30 人。")
        tables = {"doc-1": extract_tables("名单.xlsx", make_xlsx(roster_rows()))}
        stats = table_stats(chunks, "男生有多少女生有多少", tables=tables)
        self.assertIsNotNone(stats)
        self.assertIn("有效数据 30 行", stats.text)
        self.assertIn("男 18 行", stats.text)
        self.assertIn("女 12 行", stats.text)
        self.assertEqual(check_counts("男生18人，女生12人。", stats), [])
        self.assertTrue(check_counts("男生17人，女生12人。", stats))

    def test_merged_title_row_is_a_title_not_a_header(self):
        rows = [["2023 级智能科学与技术专业班级名单"],
                ["学号", "姓名", "性别"]] + roster_rows()
        chunks = doc_chunks("这是一份智能科学与技术 23-01 班的完整名单，见下表。")
        tables = {"doc-1": extract_tables("名单.xlsx", make_xlsx(rows))}
        stats = table_stats(chunks, "男生有多少女生有多少", tables=tables)
        self.assertIn("有效数据 30 行", stats.text)
        self.assertIn("男 18 行", stats.text)
        self.assertIn("标题：", stats.text)

    def test_title_row_on_a_headerless_table_is_not_data(self):
        rows = [["2023 级智能科学与技术专业班级名单"]] + roster_rows()
        chunks = doc_chunks("这是一份智能科学与技术 23-01 班的完整名单，见下表。")
        tables = {"doc-1": extract_tables("名单.xlsx", make_xlsx(rows))}
        stats = table_stats(chunks, "男生有多少女生有多少", tables=tables)
        self.assertIn("有效数据 30 行", stats.text)
        self.assertIn("男 18 行", stats.text)

    def test_headerless_table_without_ids_is_still_data(self):
        rows = [["张三", "男"], ["李四", "女"], ["王五", "男"]]
        chunks = doc_chunks("这是班里三人的名单，见下面的表格。")
        tables = {"doc-1": extract_tables("名单.xlsx", make_xlsx(rows))}
        stats = table_stats(chunks, "男生有多少", tables=tables)
        self.assertIn("有效数据 3 行", stats.text)
        self.assertIn("男 2 行", stats.text)


class OtherFormatsTest(unittest.TestCase):
    def test_docx_table_keeps_rows(self):
        rows = [["姓名", "就业状态"], ["陈畅", "求职中"], ["陈冬阳", "实习中"]]
        tables = extract_tables("台账.docx", make_docx_table(rows))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["rows"][1], ["陈畅", "求职中"])

    def test_html_table_keeps_rows(self):
        html = ("<table><tr><th>姓名</th><th>性别</th></tr>"
                "<tr><td>陈畅</td><td>男</td></tr></table>")
        tables = extract_tables("名单.html", html.encode("utf-8"), html_text=html)
        self.assertEqual(tables[0]["rows"], [["姓名", "性别"], ["陈畅", "男"]])

    def test_broken_pdf_is_not_a_table_and_does_not_raise(self):
        self.assertEqual(extract_tables("a.pdf", b"not a pdf"), [])

    def test_unknown_format_returns_nothing(self):
        self.assertEqual(extract_tables("a.txt", "姓名 | 性别".encode("utf-8")), [])


class IngestWiringTest(unittest.TestCase):
    def test_extract_document_returns_tables_with_the_text(self):
        text, meta, tables = extract_document(
            "名单.xlsx", make_xlsx(roster_rows()))
        self.assertIn("工作表：Sheet1", text)
        self.assertEqual(meta["tables"], 1)
        self.assertEqual(len(tables[0]["rows"]), 30)

    def test_library_keeps_tables_and_searcher_exposes_them(self):
        from kb import Library
        lib = Library(mode="bm25")
        coll = lib.create_collection("班级名单")
        doc = lib.create_pending_document(coll["id"], "名单.xlsx",
                                          source=".xlsx")
        text, _, tables = extract_document(
            "名单.xlsx", make_xlsx(roster_rows()))
        lib.finish_document(coll["id"], doc["id"], text, tables=tables)
        searcher = lib.searcher(coll["id"])
        self.assertEqual(len(searcher.doc_tables[doc["id"]]), 1)
        self.assertEqual(len(searcher.doc_tables[doc["id"]][0]["rows"]), 30)

    def test_reparse_keeps_the_stored_tables(self):
        from kb import Library
        lib = Library(mode="bm25")
        coll = lib.create_collection("班级名单")
        doc = lib.create_pending_document(coll["id"], "名单.xlsx",
                                          source=".xlsx")
        text, _, tables = extract_document(
            "名单.xlsx", make_xlsx(roster_rows()))
        lib.finish_document(coll["id"], doc["id"], text, tables=tables)
        lib.reparse_document(coll["id"], doc["id"])
        row = lib.get_document(coll["id"], doc["id"])
        self.assertEqual(len(row.get("tables") or []), 1)


if __name__ == "__main__":
    unittest.main()
