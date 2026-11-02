# -*- coding: utf-8 -*-
"""把主题整理成文档 / 演示大纲，并导出 Word、PPTX 与 Markdown。

生成侧只产出结构（标题、章节、要点、讲者提示），不编造引用；
导出侧用 python-docx / python-pptx，生成的文件能继续在 Word / PowerPoint
或 WPS 里改。离线演示模式给一份明确标「演示」的模板，不假装是模型写的。
"""
import datetime
import math
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
    "你是一位通用内容编辑，帮用户把主题整理成文档或演示文稿的结构。"
    "只根据用户给的主题与补充要点组织内容，不预设用户的身份、行业、"
    "学习阶段或使用场景；不要编造具体数据、论文、书名、人名或引用，"
    "拿不准就不写。只输出 JSON 对象，不要写解释或代码围栏。"
)

USER_TEMPLATE = """请为下面的主题写一份{kind_name}大纲。

要求：
1. 共 {points} 节，由浅入深，标题短而具体。
2. 每节 3-6 条要点，每条一句话，能直接讲出来；不要空话和套话。
3. 可以给每节一句讲者提示（notes），没有就留空。
4. 不要写具体统计数据、论文名、书名或「研究表明」；需要举例时用日常、通用的例子。
5. 全中文，面向普通读者；不要假设主题与考试、学校、职场或某门课程有关。
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
    return "演示文稿" if kind == "slides" else "文档"


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
    headings = (["从问题出发", "关键概念", "做法与步骤", "一个例子",
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
                "留一个可以动手验证的环节，确认真的会用。",
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
    """A4 文档：标题、章节、要点、提示，页脚写明是 AI 草稿。"""
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

DECK_THEMES = {
    # 陶土：言据默认，暖纸底 + 砖红，适合讲解、访谈、文化类主题
    "clay": {
        "BG": "FDFCF9", "INK": "1C1816", "MUTED": "6D645A",
        "BRAND": "B0501E", "ACCENT": "D4734B", "CREAM": "F5F2EA",
        "LINE": "E6DECC", "DARK": "2A211C", "ON_DARK": "FDFCF9",
        "SOFT": "EDE4D5",
    },
    # 墨蓝：冷色、克制，适合技术、产品、商业汇报
    "ink": {
        "BG": "F7F8FA", "INK": "101828", "MUTED": "5B6472",
        "BRAND": "1F4E79", "ACCENT": "4C8BB8", "CREAM": "EAF0F6",
        "LINE": "D6DEE8", "DARK": "102A43", "ON_DARK": "F7F8FA",
        "SOFT": "DCE7F1",
    },
    # 黑白：纸刊感，只有黑、白、暖灰和一点金棕
    "mono": {
        "BG": "FFFFFF", "INK": "111111", "MUTED": "666666",
        "BRAND": "8A6A3B", "ACCENT": "B08B58", "CREAM": "F2F0EB",
        "LINE": "DEDAD2", "DARK": "141414", "ON_DARK": "FFFFFF",
        "SOFT": "E8E4DC",
    },
}
DECK_STYLES = tuple(DECK_THEMES)


def _fit_size(texts: Sequence[str], width_in: float, height_in: float,
              max_size: float = 18.0, min_size: float = 11.5,
              gap_pt: float = 8.0) -> float:
    """按字数和可用面积估一个不溢出的字号；中文按一个字约一个 em 算。"""
    texts = [t for t in texts if t]
    if not texts:
        return max_size
    size = max_size
    while size >= min_size:
        chars_per_line = max(8.0, width_in * 72.0 / size * 0.96)
        lines = sum(max(1, int(math.ceil(len(t) / chars_per_line)))
                    for t in texts)
        needed = (lines * size * 1.34 +
                  max(0, len(texts) - 1) * gap_pt) / 72.0
        if needed <= height_in:
            return size
        size -= 0.5
    return min_size


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
               line_spacing: float = 1.2, anchor=None):
    from pptx.util import Pt
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    if anchor is not None:
        tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    p.line_spacing = line_spacing
    run = p.add_run()
    run.text = text
    _pptx_font(run, size, bold, color)
    return box


def render_pptx(outline: Dict, style: str = "clay") -> bytes:
    """16:9 演示文稿：多种版式交替 + 三套配色，不再是每页一样的项目符号。

    版式按章节序号轮换：左右分栏、卡片网格、步骤时间线、金句页；
    长章节自动续页，讲者提示写进备注，页脚有进度条和页码。
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Inches, Pt
    except ImportError as e:
        raise CreateError("服务器没有安装 python-pptx，暂时不能导出 PPT") from e
    out = normalize_outline(outline)
    theme = DECK_THEMES.get(str(style or "clay").lower(), DECK_THEMES["clay"])
    BG, INK, MUTED = theme["BG"], theme["INK"], theme["MUTED"]
    BRAND, ACCENT, CREAM = theme["BRAND"], theme["ACCENT"], theme["CREAM"]
    LINE, DARK, ON_DARK = theme["LINE"], theme["DARK"], theme["ON_DARK"]
    SOFT = theme["SOFT"]
    W, H = Inches(13.333), Inches(7.5)

    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    blank = prs.slide_layouts[6]

    def paint_bg(slide, color=BG):
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = RGBColor.from_string(color)

    def shape(slide, kind, left, top, width, height, fill=None, line=None,
              line_width: float = 0.75, radius=None):
        sp = slide.shapes.add_shape(kind, left, top, width, height)
        if fill:
            sp.fill.solid()
            sp.fill.fore_color.rgb = RGBColor.from_string(fill)
        else:
            sp.fill.background()
        if line:
            sp.line.color.rgb = RGBColor.from_string(line)
            sp.line.width = Pt(line_width)
        else:
            sp.line.fill.background()
        try:
            sp.shadow.inherit = False
        except Exception:
            pass
        if radius is not None and len(sp.adjustments):
            sp.adjustments[0] = radius
        return sp

    def bar(slide, left, top, width, height, color=BRAND):
        return shape(slide, MSO_SHAPE.RECTANGLE, left, top, width, height,
                     fill=color)

    def text(slide, content, left, top, width, height, size, bold=False,
             color=INK, align=PP_ALIGN.LEFT, line_spacing=1.2, anchor=None):
        return _pptx_text(slide, content, Inches(left), Inches(top),
                          Inches(width), Inches(height), size, bold, color,
                          align, line_spacing, anchor)

    def bullet_box(slide, bullets, left, top, width, height, base=18.0,
                   min_size=11.5, color=INK, marker=BRAND, gap_pt=10.0):
        from pptx.util import Inches as In
        size = _fit_size(bullets, width, height, base, min_size, gap_pt)
        box = slide.shapes.add_textbox(In(left), In(top), In(width), In(height))
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = 0
        tf.margin_right = 0
        tf.margin_top = 0
        tf.margin_bottom = 0
        for i, bullet in enumerate(bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.line_spacing = 1.24
            p.space_after = Pt(gap_pt)
            mark = p.add_run()
            mark.text = "•  "
            _pptx_font(mark, size, True, marker)
            body = p.add_run()
            body.text = bullet
            _pptx_font(body, size, False, color)
        return box

    # 总页数先算好，页脚进度条才不会越走越短
    plans = []
    for i, sec in enumerate(out["sections"]):
        chunks = [sec["bullets"][j:j + 6]
                  for j in range(0, len(sec["bullets"]), 6)] or [[""]]
        plans.append((i, sec, chunks))
    total = 2 + sum(len(p[2]) for p in plans) + 1

    def footer(slide, index: int, color=MUTED):
        text(slide, "言据 · AI 草稿，请核对事实与数据", 0.78, 6.86, 8.2, 0.3,
             9, False, color)
        text(slide, "%02d / %02d" % (index, total), 11.55, 6.86, 1.0, 0.3,
             9, False, color, PP_ALIGN.RIGHT)
        track_w = 11.78
        bar(slide, Inches(0.78), Inches(7.16), Inches(track_w),
            Inches(0.035), LINE)
        done = max(0.0, min(1.0, index / float(total)))
        if done:
            bar(slide, Inches(0.78), Inches(7.16), Inches(track_w * done),
                Inches(0.035), BRAND)

    def section_head(slide, index: int, heading: str, part: int = 0):
        text(slide, "第 %d 节" % (index + 1), 0.78, 0.52, 4.0, 0.3,
             11, True, BRAND)
        shown = heading + ("（续）" if part else "")
        size = _fit_size([shown], 9.7, 0.95, 27, 17)
        text(slide, shown, 0.75, 0.9, 9.9, 1.0, size, True, INK)
        bar(slide, Inches(0.78), Inches(1.9), Inches(0.9), Inches(0.05), BRAND)
        text(slide, "%02d" % (index + 1), 10.75, 0.14, 1.8, 1.1,
             64, True, SOFT, PP_ALIGN.RIGHT)

    # ---------- 封面 ----------
    cover = prs.slides.add_slide(blank)
    paint_bg(cover)
    bar(cover, Inches(8.55), 0, Inches(4.783), H, DARK)
    shape(cover, MSO_SHAPE.OVAL, Inches(9.15), Inches(1.05),
          Inches(3.15), Inches(3.15), fill=ACCENT)
    shape(cover, MSO_SHAPE.OVAL, Inches(9.82), Inches(1.72),
          Inches(1.8), Inches(1.8), fill=DARK)
    bar(cover, Inches(9.2), Inches(4.55), Inches(1.1), Inches(0.05), ACCENT)

    text(cover, "言据 · 创作", 0.8, 0.78, 4.0, 0.3, 12, True, BRAND)
    bar(cover, Inches(0.82), Inches(1.2), Inches(0.85), Inches(0.05), BRAND)
    cover_size = _fit_size([out["title"]], 6.7, 2.2, 40, 24, gap_pt=0)
    text(cover, out["title"], 0.8, 1.55, 6.85, 2.35, cover_size, True, INK,
         line_spacing=1.12)
    if out["subtitle"]:
        sub_size = _fit_size([out["subtitle"]], 6.6, 1.15, 15, 10.5, gap_pt=0)
        text(cover, out["subtitle"], 0.83, 4.15, 6.7, 1.2, sub_size, False,
             MUTED, line_spacing=1.45)
    text(cover, datetime.date.today().strftime("%Y 年 %m 月 %d 日"),
         0.83, 6.45, 5.0, 0.4, 11, False, MUTED)
    text(cover, "YANJU · STUDIO", 9.18, 5.55, 3.4, 0.3, 10, True, ACCENT)
    text(cover, "把主题\n变成能讲的结构", 9.18, 5.9, 3.3, 0.95, 13, False,
         ON_DARK, line_spacing=1.55)

    # ---------- 目录 ----------
    agenda = prs.slides.add_slide(blank)
    paint_bg(agenda)
    text(agenda, "目录", 0.78, 0.5, 4.0, 0.7, 30, True, INK)
    text(agenda, "CONTENTS", 0.82, 1.18, 3.0, 0.28, 10, True, BRAND)
    bar(agenda, Inches(0.82), Inches(1.52), Inches(0.9), Inches(0.05), BRAND)
    sections = out["sections"]
    cols = 2 if len(sections) > 3 else 1
    rows = int(math.ceil(len(sections) / float(cols)))
    row_h = min(0.82, 4.75 / max(1, rows))
    for i, sec in enumerate(sections[:12]):
        col, row = i % cols, i // cols
        x = 0.82 + col * 5.95
        y = 1.85 + row * row_h
        text(agenda, "%02d" % (i + 1), x, y, 0.55, 0.35, 12, True, BRAND)
        size = _fit_size([sec["heading"]], 4.7, 0.38, 15.5, 10.5, gap_pt=0)
        text(agenda, sec["heading"], x + 0.58, y - 0.02, 4.75, 0.4,
             size, False, INK)
        bar(agenda, Inches(x), Inches(y + row_h - 0.18),
            Inches(5.25), Inches(0.012), LINE)
    footer(agenda, 2)

    # ---------- 章节页 ----------
    number = 2
    for i, sec, chunks in plans:
        for part, bullets in enumerate(chunks):
            number += 1
            slide = prs.slides.add_slide(blank)
            paint_bg(slide)
            heading = sec["heading"] + ("（续）" if part else "")
            longest = max([len(b) for b in bullets] or [0])

            if part:
                # 续页用两栏列表，信息密度高，不和首页抢版式
                section_head(slide, i, sec["heading"], part)
                if len(bullets) >= 4:
                    half = int(math.ceil(len(bullets) / 2.0))
                    bullet_box(slide, bullets[:half], 0.85, 2.15, 5.55, 4.35,
                               16, 11.5)
                    bullet_box(slide, bullets[half:], 6.95, 2.15, 5.55, 4.35,
                               16, 11.5)
                else:
                    bullet_box(slide, bullets, 0.85, 2.15, 11.7, 4.35,
                               18, 12)
                footer(slide, number)
            else:
                layout = ["split", "cards", "steps", "quote"][i % 4]
                if longest > 90:
                    layout = "split"
                if len(bullets) == 1 and longest >= 24:
                    layout = "quote"
                if layout == "steps" and (len(bullets) > 5 or longest > 52):
                    layout = "cards"
                if layout == "quote" and (not bullets or
                                          len(bullets[0]) < 16):
                    layout = "cards"

                if layout == "split":
                    bar(slide, 0, 0, Inches(4.35), H, DARK)
                    bar(slide, Inches(0.62), Inches(1.0), Inches(0.9),
                        Inches(0.05), ACCENT)
                    text(slide, "第 %d 节" % (i + 1), 0.64, 1.15, 3.0, 0.3,
                         11, True, ACCENT)
                    text(slide, "%02d" % (i + 1), 0.6, 1.5, 3.2, 1.0,
                         58, True, ACCENT)
                    hsize = _fit_size([sec["heading"]], 3.15, 2.0, 24, 15,
                                      gap_pt=0)
                    text(slide, sec["heading"], 0.64, 2.9, 3.2, 2.15,
                         hsize, True, ON_DARK, line_spacing=1.18)
                    text(slide, "SECTION", 0.66, 6.45, 2.4, 0.3, 9, True,
                         ACCENT)
                    text(slide, "本节要点", 4.85, 0.82, 3.0, 0.3, 11, True,
                         BRAND)
                    bar(slide, Inches(4.87), Inches(1.14), Inches(0.62),
                        Inches(0.05), BRAND)
                    bullet_box(slide, bullets, 4.85, 1.5, 7.65, 4.95,
                               18, 12.5, gap_pt=12)
                    footer(slide, number)

                elif layout == "cards":
                    section_head(slide, i, sec["heading"])
                    n = len(bullets)
                    cols = 2 if n > 1 else 1
                    rows = int(math.ceil(n / float(cols)))
                    gap = 0.16
                    card_w = (11.75 - gap * (cols - 1)) / cols
                    card_h = (4.25 - gap * (rows - 1)) / max(1, rows)
                    for j, bullet in enumerate(bullets):
                        c, r = j % cols, j // cols
                        x = 0.78 + c * (card_w + gap)
                        y = 2.12 + r * (card_h + gap)
                        shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE,
                              Inches(x), Inches(y), Inches(card_w),
                              Inches(card_h), fill=CREAM, line=LINE,
                              line_width=0.75, radius=0.055)
                        bar(slide, Inches(x + 0.02), Inches(y + 0.14),
                            Inches(0.045), Inches(card_h - 0.28), ACCENT)
                        text(slide, "%02d" % (j + 1), x + 0.18, y + 0.1,
                             0.8, 0.26, 10, True, BRAND)
                        size = _fit_size([bullet], card_w - 0.4,
                                         card_h - 0.42, 15, 10.5, gap_pt=0)
                        text(slide, bullet, x + 0.18, y + 0.38,
                             card_w - 0.36, card_h - 0.46, size, False,
                             INK, line_spacing=1.28)
                    footer(slide, number)

                elif layout == "steps":
                    section_head(slide, i, sec["heading"])
                    n = len(bullets)
                    row_h = min(0.84, 4.05 / max(1, n))
                    top = 2.22
                    if n > 1:
                        bar(slide, Inches(1.09), Inches(top + 0.22),
                            Inches(0.025), Inches(row_h * (n - 1)), LINE)
                    for j, bullet in enumerate(bullets):
                        y = top + j * row_h
                        shape(slide, MSO_SHAPE.OVAL, Inches(0.82),
                              Inches(y), Inches(0.52), Inches(0.52),
                              fill=BRAND)
                        text(slide, str(j + 1), 0.82, y + 0.005, 0.52, 0.5,
                             10.5, True, ON_DARK, PP_ALIGN.CENTER,
                             anchor=MSO_ANCHOR.MIDDLE)
                        size = _fit_size([bullet], 10.6, row_h - 0.12,
                                         16, 11)
                        text(slide, bullet, 1.55, y - 0.02, 10.85,
                             row_h - 0.06, size, False, INK,
                             anchor=MSO_ANCHOR.MIDDLE)
                    footer(slide, number)

                else:  # quote
                    section_head(slide, i, sec["heading"])
                    shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE,
                          Inches(0.78), Inches(2.02), Inches(11.75),
                          Inches(2.6), fill=CREAM, line=LINE,
                          line_width=0.75, radius=0.035)
                    text(slide, "“", 0.95, 1.55, 1.0, 1.0, 72, True, ACCENT)
                    quote = bullets[0]
                    qsize = _fit_size([quote], 10.25, 1.95, 28, 17, gap_pt=0)
                    text(slide, quote, 1.62, 2.18, 10.3, 2.25, qsize, False,
                         INK, line_spacing=1.32)
                    rest = bullets[1:]
                    if rest:
                        half = int(math.ceil(len(rest) / 2.0))
                        bullet_box(slide, rest[:half], 0.88, 4.9, 5.45, 1.55,
                                   13.5, 10.5, gap_pt=6)
                        if rest[half:]:
                            bullet_box(slide, rest[half:], 6.85, 4.9, 5.45,
                                       1.55, 13.5, 10.5, gap_pt=6)
                    footer(slide, number)

            if sec.get("notes") and part == 0:
                try:
                    slide.notes_slide.notes_text_frame.text = sec["notes"]
                except Exception:
                    pass

    # ---------- 结束页 ----------
    end = prs.slides.add_slide(blank)
    paint_bg(end, DARK)
    shape(end, MSO_SHAPE.OVAL, Inches(9.6), Inches(-1.45),
          Inches(5.4), Inches(5.4), fill=ACCENT)
    shape(end, MSO_SHAPE.OVAL, Inches(10.55), Inches(-0.5),
          Inches(3.5), Inches(3.5), fill=DARK)
    text(end, "言据 · 创作", 0.85, 0.85, 4.0, 0.3, 12, True, ACCENT)
    text(end, "谢谢", 0.8, 2.15, 6.0, 1.1, 48, True, ON_DARK)
    bar(end, Inches(0.85), Inches(3.45), Inches(1.15), Inches(0.06), ACCENT)
    text(end, "这份演示是 AI 草稿，数据与事实请再核对一遍。",
         0.85, 3.72, 7.6, 0.8, 15, False, ON_DARK, line_spacing=1.45)
    if out["title"]:
        text(end, "主题 · " + out["title"], 0.85, 6.45, 7.8, 0.4, 10,
             False, ACCENT)

    import io
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
