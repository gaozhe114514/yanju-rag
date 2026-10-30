# -*- coding: utf-8 -*-
"""本地文件解析：把上传的文件变成可以切分的纯文本。

只做「把文字取出来」这一件事，不做版面还原：
- PDF 用 pypdf 取文字层（扫描件没有文字层时，可点「重切分」前先转成图片再传）
- Word / PPT 是 zip + XML，用标准库拆，不额外装 python-docx / python-pptx
- TXT / Markdown / CSV / JSON / HTML 按文本读，HTML 用 bs4 去标签
- 图片（png/jpg/webp…）走本地 OCR（rapidocr-onnxruntime，CPU 推理）；
  引擎没装或关闭时明确说明，不假装识别成功

解析结果就是纯文本，后续切分、向量化、引用校验全部走同一条本地链路。
"""
import io
import os
import re
import threading
import zipfile
from typing import Optional, Tuple

MAX_BYTES = 20 * 1024 * 1024
MAX_CHARS = 50000
# 长边先缩到这个像素再识别：手机拍的原图动辄 4000px，缩小后准确率几乎不变，速度快一截
MAX_IMAGE_SIDE = 2400
MAX_IMAGE_PIXELS = 40_000_000
OCR_MIN_SCORE = 0.5

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log"}
HTML_EXT = {".html", ".htm", ".xhtml"}
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx"}
PPTX_EXT = {".pptx"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
LEGACY_EXT = {".doc", ".ppt", ".xls"}

SUPPORTED_EXT = TEXT_EXT | HTML_EXT | PDF_EXT | DOCX_EXT | PPTX_EXT | IMAGE_EXT

# 解析失败时给用户看的原因；不写 Python 异常类型，写得能照着处理
_EMPTY_HINTS = {
    ".pdf": "PDF 里没有文字层（多半是扫描件或截图），"
            "可以把它导出成图片再上传，这边会走 OCR",
    ".docx": "文档里没有可提取的文字，检查是不是只有图片",
    ".pptx": "幻灯片里没有可提取的文字，检查是不是只有图片",
    ".html": "网页里没有正文文字",
}

# 图片识别不到字时，统一给这一句（png/jpg/webp/bmp/gif/tif 都适用）
_OCR_EMPTY_HINT = "图片里没识别出文字：可能拍得太小、太糊，或者图里本来就没有字"

_OCR_LOCK = threading.Lock()
_OCR_ENGINE = None


def _ocr_enabled() -> bool:
    return os.environ.get("KB_OCR", "1").strip().lower() not in ("0", "false", "off", "no")


def _ocr_engine():
    """懒加载 OCR 引擎：第一次传图片才把模型读进内存，之后复用。"""
    global _OCR_ENGINE
    if not _ocr_enabled():
        raise ParseError("服务端关闭了图片识别（KB_OCR=0），请把图里的文字转成文本再上传")
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                raise ParseError("服务端没有安装 OCR 引擎（rapidocr-onnxruntime），"
                                 "暂时不能识别图片；可以先上传文字版")
            try:
                _OCR_ENGINE = RapidOCR()
            except Exception as e:
                raise ParseError("OCR 引擎加载失败：%s" % e)
    return _OCR_ENGINE


def _image_array(data: bytes):
    """把上传的图片读成 RGB 数组，顺手挡住超大图和坏文件。"""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        raise ParseError("服务端缺少 Pillow / numpy，暂时不能处理图片")
    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "is_animated", False):
            img.seek(0)
        img.load()
    except Exception as e:
        raise ParseError("图片打不开（文件可能损坏或不是图片）：%s" % e)
    img = img.convert("RGB")
    width, height = img.size
    if width * height > MAX_IMAGE_PIXELS:
        raise ParseError("图片分辨率太大（%d×%d 像素），先缩小再上传" % (width, height))
    longest = max(width, height)
    if longest > MAX_IMAGE_SIDE:
        scale = float(MAX_IMAGE_SIDE) / longest
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                         Image.LANCZOS)
    return np.array(img)


def _ocr_text(data: bytes) -> str:
    engine = _ocr_engine()
    image = _image_array(data)
    try:
        out = engine(image)
    except Exception as e:
        raise ParseError("图片识别失败：%s" % e)
    # rapidocr 1.x 返回 (result, elapse)，result 是 [[box, text, score], …] 或 None
    result = out[0] if isinstance(out, tuple) and len(out) == 2 else out
    lines = []
    for item in result or []:
        if not item or len(item) < 2:
            continue
        text = item[1]
        score = 1.0
        if len(item) > 2:
            try:
                score = float(item[2])
            except (TypeError, ValueError):
                score = 1.0
        if text and score >= OCR_MIN_SCORE:
            lines.append(str(text))
    return "\n".join(lines)


class ParseError(ValueError):
    """解析失败的统一错误，message 直接给用户看。"""


def extension_of(name: str) -> str:
    name = (name or "").strip().lower()
    if "." not in name.rsplit("/", 1)[-1]:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def _decode(data: bytes, name: str) -> str:
    """按常见中文编码依次尝试；都不行时用 chardet 猜一次。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        import chardet
        guess = (chardet.detect(data[:20000]) or {}).get("encoding")
        if guess:
            return data.decode(guess, errors="replace")
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_text(data: bytes, name: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ParseError("服务端缺少 bs4，暂时不能解析 HTML")
    soup = BeautifulSoup(_decode(data, name), "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text("\n")


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ParseError("服务端缺少 pypdf，暂时不能解析 PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise ParseError("PDF 打不开（文件可能损坏或加了密）：%s" % e)
    if getattr(reader, "is_encrypted", False):
        raise ParseError("PDF 加了密码，先解密再上传")
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages)


def _xml_texts(xml: bytes, tag: str) -> list:
    """从 zip 里的 XML 取指定标签的文字（Word 是 w:t，PPT 是 a:t）。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter():
        if node.tag.endswith("}" + tag) or node.tag == tag:
            if node.text:
                out.append(node.text)
    return out


def _docx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ParseError("Word 文件打不开（可能是老的 .doc 改了后缀，或者文件损坏）")
    with zf:
        if "word/document.xml" not in zf.namelist():
            raise ParseError("这不是标准的 .docx 文件")
        xml = zf.read("word/document.xml")
    # 段落之间要留换行，否则整篇会粘成一行，切分和引用都会跑偏
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ParseError("Word 正文解析失败")
    paras = []
    for p in root.iter():
        if p.tag.endswith("}p"):
            words = _xml_texts(ET.tostring(p), "t")
            if words:
                paras.append("".join(words))
    if not paras:
        paras = _xml_texts(xml, "t")
    return "\n".join(paras)


def _pptx_text(data: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ParseError("PPT 文件打不开（可能是老的 .ppt 改了后缀，或者文件损坏）")
    with zf:
        names = [n for n in zf.namelist()
                 if re.match(r"ppt/slides/slide\d+\.xml$", n)]

        def page_no(n):
            m = re.search(r"slide(\d+)\.xml$", n)
            return int(m.group(1)) if m else 0

        slides = []
        for n in sorted(names, key=page_no):
            words = _xml_texts(zf.read(n), "t")
            if words:
                slides.append("第 %d 页\n%s" % (page_no(n), " ".join(words)))
    return "\n\n".join(slides)


def extract_text(name: str, data: bytes) -> Tuple[str, dict]:
    """返回 (正文, 元信息)。失败抛 ParseError，message 直接展示给用户。"""
    if not data:
        raise ParseError("文件是空的")
    if len(data) > MAX_BYTES:
        raise ParseError("文件太大，单篇最多 %d MB" % (MAX_BYTES // 1024 // 1024))
    ext = extension_of(name)
    if ext in LEGACY_EXT:
        raise ParseError("不支持老的 %s 格式，请另存为 .docx / .pptx 再上传" % ext)
    if ext not in SUPPORTED_EXT:
        raise ParseError("不支持这种文件（%s）。支持 PDF / Word(.docx) / "
                         "PPT(.pptx) / 图片(PNG / JPG / WebP) / Markdown / TXT / HTML / CSV / JSON"
                         % (ext or "没有扩展名"))

    meta = {"ext": ext, "bytes": len(data)}
    if ext in PDF_EXT:
        text = _pdf_text(data)
    elif ext in DOCX_EXT:
        text = _docx_text(data)
    elif ext in PPTX_EXT:
        text = _pptx_text(data)
    elif ext in HTML_EXT:
        text = _html_text(data, name)
    elif ext in IMAGE_EXT:
        text = _ocr_text(data)
        meta["ocr"] = True
    else:
        text = _decode(data, name)

    text = _clean(text)
    if len(text) < 20:
        if ext in IMAGE_EXT:
            raise ParseError(_OCR_EMPTY_HINT)
        raise ParseError(_EMPTY_HINTS.get(ext, "没提取到足够的文字（至少 20 字）"))
    if len(text) > MAX_CHARS:
        raise ParseError("提取到 %d 字，超过单篇 %d 字上限，请拆分后再上传"
                         % (len(text), MAX_CHARS))
    return text, meta
