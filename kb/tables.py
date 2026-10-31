# -*- coding: utf-8 -*-
"""表格的结构化抽取：把 xlsx / docx / pdf / html 里的表格读成「行 × 列」。

正文是给检索和引用看的：一行「542313310101 | 陈畅 | 男」人读起来很清楚，
但服务端想数「男生几人」时，只能从这段文字反推表格。没有表头、有合并单元格、
中间空了一列，反推就要靠猜——线上那份名单就是这么把第一行当成表头的。

结构化表格在进料那一刻把行列固定下来，统计直接数行，不再依赖正文格式。
引擎用各格式最成熟的库，而不是自己拆 XML：

- xlsx：openpyxl（合并单元格、日期、公式缓存值都能拿到）
- docx：python-docx（真正的表格对象，不是把单元格拍平成段落）
- pdf：pdfplumber（按线框还原行列；没有线框的 PDF 由调用方退回文字解析）
- html：bs4（按 tr / td 取表格）

任何一个引擎没装、文件损坏或抽不出来，都只是「这篇没有结构化表格」：
返回空列表，正文照常入库。表格是锦上添花，不能因为表抽不出来把整份资料丢掉。
"""
import io
import re
from datetime import date, datetime
from typing import List, Optional

# 一份资料最多抽几张表、每张多少行多少列：20MB 的表格书也可能有几百张表，
# 全抽进内存和数据库没有意义，统计类问题也只关心前几张。
MAX_TABLES = 20
MAX_ROWS = 500
MAX_COLS = 40
# PDF 抽表按页做，慢；前 40 页通常已经覆盖了用户会问的表格
MAX_PAGES = 40
# 线上容器只有 3.6GB 内存，精排模型已经占掉 2GB+：大 PDF 交给 pdfplumber
# 逐页建对象容易把容器顶到 OOM。超过这个体积就不抽表，正文由 pypdf 照常取。
MAX_PDF_BYTES = 8 * 1024 * 1024


def _cell_text(value) -> str:
    """单元格 → 单行文字。换行压成空格，日期写成 ISO，整数不带 .0。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        if value.time() == datetime.min.time():
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return ("%.10f" % value).rstrip("0").rstrip(".")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip()


def _clean_rows(rows) -> List[List[str]]:
    """去掉整行为空的行、行尾的空列，并卡住列数与行数。"""
    out: List[List[str]] = []
    for raw in rows:
        cells = [_cell_text(v) for v in raw][:MAX_COLS]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        out.append(cells)
        if len(out) >= MAX_ROWS:
            break
    return out


def _usable(rows: List[List[str]]) -> bool:
    """至少两行两列才算一张表：一行是标题、一列是清单的情况交给正文处理。"""
    return len(rows) >= 2 and max((len(r) for r in rows), default=0) >= 2


def _xlsx_tables(data: bytes) -> List[dict]:
    try:
        import openpyxl
    except ImportError:
        return []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                    data_only=True)
    except Exception:
        return []
    out: List[dict] = []
    try:
        for ws in wb.worksheets:
            try:
                rows = _clean_rows(ws.iter_rows(values_only=True))
            except Exception:
                continue
            if _usable(rows):
                out.append({"sheet": ws.title or "", "rows": rows,
                            "source": "xlsx"})
            if len(out) >= MAX_TABLES:
                break
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return out


def _docx_tables(data: bytes) -> List[dict]:
    try:
        from docx import Document
    except ImportError:
        return []
    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        return []
    out: List[dict] = []
    try:
        for table in doc.tables:
            rows = _clean_rows([[cell.text for cell in row.cells]
                                for row in table.rows])
            if _usable(rows):
                out.append({"sheet": "", "rows": rows, "source": "docx"})
            if len(out) >= MAX_TABLES:
                break
    except Exception:
        return out
    return out


def _pdf_tables(data: bytes) -> List[dict]:
    if len(data) > MAX_PDF_BYTES:
        return []
    try:
        import pdfplumber
    except ImportError:
        return []
    out: List[dict] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:MAX_PAGES]:
                try:
                    found = page.extract_tables() or []
                except Exception:
                    continue
                for table in found:
                    rows = _clean_rows(table)
                    if _usable(rows):
                        out.append({"sheet": "第 %d 页" % page.page_number,
                                    "rows": rows, "source": "pdf"})
                    if len(out) >= MAX_TABLES:
                        return out
    except Exception:
        return out
    return out


def _html_tables(text: str) -> List[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return []
    out: List[dict] = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True)
                     for c in tr.find_all(["td", "th"])]
            rows.append(cells)
        rows = _clean_rows(rows)
        if _usable(rows):
            out.append({"sheet": "", "rows": rows, "source": "html"})
        if len(out) >= MAX_TABLES:
            break
    return out


TABLE_EXT = {".xlsx", ".docx", ".pdf", ".html", ".htm", ".xhtml"}


def extract_tables(name: str, data: bytes,
                   html_text: Optional[str] = None) -> List[dict]:
    """按扩展名抽结构化表格；不支持的格式、抽不出来的一律返回 []。

    返回 [{"sheet": 工作表/页码, "rows": [[单元格, …], …], "source": "xlsx"}]。
    行里的值已经去空白、去行尾空列；表头不在这里判断，交给统计层
    （同一份规则只写在 answer/aggregate.py 一处）。

    html_text 是调用方已经按编码探测解好的网页源码：bs4 自己猜 GBK 不靠谱，
    和正文用同一份解码结果，表格里的字才和引用对得上。
    """
    if not data:
        return []
    lower = (name or "").lower()
    if "." not in lower:
        return []
    ext = lower[lower.rfind("."):]
    if ext not in TABLE_EXT:
        return []
    try:
        if ext == ".xlsx":
            return _xlsx_tables(data)
        if ext == ".docx":
            return _docx_tables(data)
        if ext == ".pdf":
            return _pdf_tables(data)
        if html_text is None:
            return []
        return _html_tables(html_text)
    except Exception:
        # 引擎内部炸了就当作没有表格：正文解析已经成功，不能因此丢资料
        return []
