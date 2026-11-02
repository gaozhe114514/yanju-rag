# -*- coding: utf-8 -*-
"""把主题整理成文档 / 演示大纲，并导出 Word、PPTX 与 Markdown。

生成侧只产出结构（标题、章节、要点、讲者提示），不编造引用；
导出侧用 python-docx / python-pptx，生成的文件能继续在 Word / PowerPoint
或 WPS 里改。离线演示模式给一份明确标「演示」的模板，不假装是模型写的。
"""
import datetime
import re
from typing import Any, Dict, List, Optional, Sequence

from .providers import ProviderError, parse_payload

KINDS = ("doc", "slides")
MAX_SECTIONS = 12
MIN_SECTIONS = 2
MAX_BULLETS = 8
MAX_TITLE = 80
MAX_SUBTITLE = 160
MAX_HEADING = 60
MAX_BULLET = 200
MAX_NOTES = 300

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SYSTEM_PROMPT = (
    "你是给考研学生写讲义和演示文稿的编辑。你只根据用户给的主题与补充要点"
    "组织内容，不编造具体数据、论文、书名、人名或引用；拿不准就不写。"
    "只输出 JSON 对象，不要写解释或代码围栏。"
)

USER_TEMPLATE = """请为下面的主题写一份{kind_name}大纲。

要求：
1. 共 {points} 节，由浅入深，标题短而具体。
2. 每节 3-6 条要点，每条一句话，能直接讲出来；不要空话和套话。
3. 可以给每节一句讲者提示（notes），没有就留空。
4. 不要写具体统计数据、论文名、书名或「研究表明」；需要举例时用通用例子。
5. 全中文，面向考研复习。
6. 只输出 JSON：
{{"title": "", "subtitle": "", "sections": [
  {{"heading": "", "bullets": ["", ""], "notes": ""}}
]}}

主题：{topic}
补充要点：
<<<
{notes}
>>>
"""


class CreateError(ValueError):
    """大纲生成或校验失败（对用户可读）。"""


def _clean(value: Any, limit: int) -> str:
    text = _CTRL.sub("", str(value or ""))
    text = _WS.sub(" ", text).strip()
    return text[:limit]


def _kind_name(kind: str) -> str:
    return "演示文稿" if kind == "slides" else "学习文档"


def _bullets(raw: Any, heading: str) -> List[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        text = _clean(item, MAX_BULLET)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_BULLETS:
            break
    return out


def normalize_outline(raw: Any, fallback_title: str = "") -> Dict:
    """把模型 / 客户端给的 JSON 收成固定形状，越界一律截断。"""
    if not isinstance(raw, dict):
        raise CreateError("大纲格式不对")
    kind = str(raw.get("kind") or "doc").strip().lower()
    if kind not in KINDS:
        kind = "doc"
    title = _clean(raw.get("title"), MAX_TITLE) or _clean(fallback_title, MAX_TITLE)
    if not title:
        title = "未命名"
    subtitle = _clean(raw.get("subtitle"), MAX_SUBTITLE)
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list):
        raise CreateError("大纲里没有章节")
    sections: List[Dict] = []
    for item in sections_raw[:MAX_SECTIONS]:
        if not isinstance(item, dict):
            continue
        heading = _clean(item.get("heading") or item.get("title"), MAX_HEADING)
        bullets = _bullets(item.get("bullets") or item.get("points"), heading)
        if not heading or not bullets:
            continue
        sections.append({
            "heading": heading,
            "bullets": bullets,
            "notes": _clean(item.get("notes"), MAX_NOTES),
        })
    if len(sections) < MIN_SECTIONS:
        raise CreateError("大纲至少要有两节，每节至少一条要点")
    return {"kind": kind, "title": title, "subtitle": subtitle,
            "sections": sections}


def _demo_points(topic: str, notes: str) -> List[str]:
    parts = re.split(r"[\n；;。]+", notes or "")
    return [p.strip(" -·\t") for p in parts if len(p.strip(" -·\t")) >= 2][:24]


def demo_outline(topic: str, kind: str = "doc", points: int = 5,
                 notes: str = "") -> Dict:
    """离线演示：结构是真的，内容只是把主题和补充要点排进去，明确标 demo。"""
    topic = _clean(topic, MAX_TITLE) or "未命名主题"
    kind = kind if kind in KINDS else "doc"
    try:
        count = max(MIN_SECTIONS, min(int(points or 5), 10))
    except (TypeError, ValueError):
        count = 5
    headings = (["背景与目标", "核心概念", "方法与步骤", "例子与练习",
                 "常见误区", "小结与下一步"] if kind == "doc"
                else ["为什么讲这个", "核心概念", "方法与步骤",
                      "一个例子", "小结"])
    if count > len(headings):
        headings += ["补充 %d" % i for i in range(2, count - len(headings) + 2)]
    headings = headings[:count]
    pool = _demo_points(topic, notes)
    per = max(2, (len(pool) + count - 1) // count) if pool else 0
    sections = []
    for index, heading in enumerate(headings):
        own = pool[index * per:(index + 1) * per] if per else []
        if own:
            bullets = own
        elif kind == "doc":
            bullets = [
                "围绕「%s」说清这一节要解决的问题。" % topic,
                "把关键词、前提和结论分开写，方便回看。",
                "留一处练习或自测，确认真的会用。",
            ]
        else:
            bullets = [
                "这一节只讲「%s」的一个重点。" % topic,
                "用一句话说清是什么、为什么、怎么用。",
            ]
        sections.append({
            "heading": heading,
            "bullets": bullets[:MAX_BULLETS],
            "notes": "演示结构：把补充要点换成你自己的内容再讲。",
        })
    return normalize_outline({
        "kind": kind,
        "title": topic,
        "subtitle": "AI 演示草稿 · 共 %d 节 · 请核对并补充事实" % len(sections),
        "sections": sections,
    }, fallback_title=topic)


def build_messages(topic: str, kind: str, points: int, notes: str) -> List[Dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(
            kind_name=_kind_name(kind), points=points,
            topic=_clean(topic, MAX_TITLE), notes=_clean(notes, 2000))},
    ]


def generate_outline(provider, topic: str, kind: str = "doc", points: int = 5,
                     notes: str = "", demo: bool = False) -> Dict:
    """有模型就让模型写；离线或调用失败时退回明确标注的演示模板。"""
    if demo or provider is None or not hasattr(provider, "complete"):
        return {"outline": demo_outline(topic, kind, points, notes), "demo": True}
    messages = build_messages(topic, kind, points, notes)
    last: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            raw = provider.complete(messages, temperature=0.55,
                                    max_tokens=2600, json_mode=True)
            payload = parse_payload(raw)
            payload["kind"] = kind
            return {"outline": normalize_outline(payload, fallback_title=topic),
                    "demo": False}
        except (ProviderError, CreateError) as e:
            last = e
            if attempt == 1:
                messages = messages + [{
                    "role": "user",
                    "content": "上一次输出不能用（%s）。请只输出合法 JSON，"
                               "标题、章节和每条要点都要写全。" % e,
                }]
    raise CreateError("模型没有给出可用大纲：%s" % last)


def render_markdown(outline: Dict) -> str:
    out = normalize_outline(outline)
    lines = ["# " + out["title"], ""]
    if out["subtitle"]:
        lines += ["> " + out["subtitle"], ""]
    for sec in out["sections"]:
        lines += ["## " + sec["heading"], ""]
        lines += ["- " + b for b in sec["bullets"]]
        if sec.get("notes"):
            lines += ["", "> 讲者提示：" + sec["notes"]]
        lines.append("")
    lines += ["---", "", "由言据生成 · AI 草稿，请核对事实与数据。", ""]
    return "\n".join(lines)


def safe_filename(title: str, ext: str) -> str:
    name = _CTRL.sub("", str(title or "")).strip()
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = _WS.sub(" ", name).strip(" .")
    name = name[:60].strip(" .") or "言据创作"
    return name + "." + ext.lstrip(".")


# ---------- Word ----------

def _docx_set_font(run, size: float, bold: bool = False,
                   color: str = "1C1816", name: str = "Microsoft YaHei"):
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:eastAsia"), name)


def render_docx(outline: Dict) -> bytes:
    """A4 讲义：标题、章节、要点、讲者提示，页脚写明是 AI 草稿。"""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as e:
        raise CreateError("服务器没有安装 python-docx，暂时不能导出 Word") from e
    out = normalize_outline(outline)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)

    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(4)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_font(title.add_run(out["title"]), 24, True, "1C1816")
    title.paragraph_format.space_after = Pt(6)

    if out["subtitle"]:
        sub = doc.add_paragraph()
        _docx_set_font(sub.add_run(out["subtitle"]), 11, False, "6D645A")
        sub.paragraph_format.space_after = Pt(14)

    for sec in out["sections"]:
        head = doc.add_paragraph()
        head.paragraph_format.space_before = Pt(14)
        head.paragraph_format.space_after = Pt(4)
        _docx_set_font(head.add_run(sec["heading"]), 15, True, "B0501E")
        for bullet in sec["bullets"]:
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.line_spacing = 1.5
            _docx_set_font(p.add_run(bullet), 11, False, "1C1816")
        if sec.get("notes"):
            note = doc.add_paragraph()
            note.paragraph_format.space_before = Pt(2)
            _docx_set_font(note.add_run("讲者提示：" + sec["notes"]),
                           9.5, False, "8B8175")

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _docx_set_font(footer.add_run(
        "言据 · AI 草稿，请核对事实与数据 · %s" %
        datetime.date.today().isoformat()), 9, False, "8B8175")

    import io
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------- PowerPoint ----------

def _pptx_font(run, size: float, bold: bool = False, color: str = "1C1816",
               name: str = "Microsoft YaHei"):
    from pptx.dml.color import RGBColor
    from pptx.oxml.ns import qn
    from pptx.util import Pt
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        rpr.append(ea)
    ea.set("typeface", name)


def _pptx_text(slide, text: str, left, top, width, height, size: float,
               bold: bool = False, color: str = "1C1816", align=None,
               line_spacing: float = 1.15):
    from pptx.util import Pt
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    p.line_spacing = line_spacing
    run = p.add_run()
    run.text = text
    _pptx_font(run, size, bold, color)
    return box


def render_pptx(outline: Dict) -> bytes:
    """16:9 演示文稿：封面、目录、章节页、结束页，配色沿用言据的陶土/米白。"""
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Emu, Inches, Pt
    except ImportError as e:
        raise CreateError("服务器没有安装 python-pptx，暂时不能导出 PPT") from e
    out = normalize_outline(outline)
    BG, INK, MUTED = "FDFCF9", "1C1816", "6D645A"
    BRAND, CREAM, LINE = "B0501E", "F5F2EA", "E6DECC"
    W, H = Inches(13.333), Inches(7.5)

    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    blank = prs.slide_layouts[6]

    def paint_bg(slide, color=BG):
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = RGBColor.from_string(color)

    def bar(slide, left, top, width, height, color=BRAND):
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string(color)
        shape.line.fill.background()
        return shape

    def footer(slide, index: int):
        _pptx_text(slide, "言据 · AI 草稿，请核对事实与数据",
                   Inches(0.75), Inches(6.95), Inches(8), Inches(0.3), 9, False, MUTED)
        _pptx_text(slide, str(index), Inches(12.1), Inches(6.95),
                   Inches(0.55), Inches(0.3), 9, False, MUTED, PP_ALIGN.RIGHT)

    # 封面
    cover = prs.slides.add_slide(blank)
    paint_bg(cover)
    bar(cover, Inches(0.75), Inches(1.15), Inches(0.12), Inches(2.0), BRAND)
    _pptx_text(cover, "言据 · 创作", Inches(1.15), Inches(1.1),
               Inches(4), Inches(0.3), 12, True, BRAND)
    _pptx_text(cover, out["title"], Inches(1.1), Inches(1.5),
               Inches(11.2), Inches(1.5), 36, True, INK)
    if out["subtitle"]:
        _pptx_text(cover, out["subtitle"], Inches(1.15), Inches(3.15),
                   Inches(10.8), Inches(1.0), 15, False, MUTED)
    _pptx_text(cover, datetime.date.today().strftime("%Y 年 %m 月 %d 日"),
               Inches(1.15), Inches(6.1), Inches(5), Inches(0.4), 11, False, MUTED)

    # 目录
    agenda = prs.slides.add_slide(blank)
    paint_bg(agenda)
    _pptx_text(agenda, "目录", Inches(0.75), Inches(0.55),
               Inches(4), Inches(0.6), 26, True, INK)
    bar(agenda, Inches(0.75), Inches(1.25), Inches(0.9), Inches(0.05), BRAND)
    top = Inches(1.65)
    step = Inches(0.48)
    for i, sec in enumerate(out["sections"][:10]):
        _pptx_text(agenda, "%02d" % (i + 1), Inches(0.78), top + step * i,
                   Inches(0.6), Inches(0.4), 12, True, BRAND)
        _pptx_text(agenda, sec["heading"], Inches(1.45), top + step * i,
                   Inches(10.8), Inches(0.4), 16, False, INK)
    footer(agenda, 2)

    # 章节页：每页最多 6 条，超出拆成续页
    number = 2
    for i, sec in enumerate(out["sections"]):
        chunks = [sec["bullets"][j:j + 6]
                  for j in range(0, len(sec["bullets"]), 6)] or [[""]]
        for part, bullets in enumerate(chunks):
            number += 1
            slide = prs.slides.add_slide(blank)
            paint_bg(slide)
            _pptx_text(slide, "第 %d 节" % (i + 1), Inches(0.75), Inches(0.55),
                       Inches(3), Inches(0.3), 11, True, BRAND)
            heading = sec["heading"] + ("（续）" if part else "")
            _pptx_text(slide, heading, Inches(0.75), Inches(0.9),
                       Inches(11.6), Inches(0.8), 28, True, INK)
            bar(slide, Inches(0.75), Inches(1.78), Inches(0.9), Inches(0.05), BRAND)
            top = Inches(2.15)
            for j, bullet in enumerate(bullets):
                _pptx_text(slide, "•  " + bullet, Inches(0.9),
                           top + Inches(0.72) * j, Inches(11.4),
                           Inches(0.62), 17, False, INK, line_spacing=1.2)
            if sec.get("notes") and part == 0:
                try:
                    slide.notes_slide.notes_text_frame.text = sec["notes"]
                except Exception:
                    pass
            footer(slide, number)

    # 结束页
    end = prs.slides.add_slide(blank)
    paint_bg(end, CREAM)
    bar(end, Inches(0.75), Inches(2.3), Inches(0.12), Inches(1.4), BRAND)
    _pptx_text(end, "谢谢", Inches(1.15), Inches(2.15),
               Inches(5), Inches(0.9), 34, True, INK)
    _pptx_text(end, "这份 PPT 是 AI 草稿，数据与事实请再核对一遍。",
               Inches(1.15), Inches(3.15), Inches(10), Inches(0.6), 15, False, MUTED)

    import io
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
