# -*- coding: utf-8 -*-
"""修复 PDF 抽取产生的私有区字符（Symbol 字体）。

pdftotext 常把数学符号抽成 U+F0xx：看上去是空白或方块，实际是字体私有编码。
这里按 Symbol 字体映射回真实符号；映射不了的保留原字符，由 validate 报 E。
"""

# Symbol 字体：0xF000 + ASCII 码位
SYMBOLS = {
    0xF02B: "+",
    0xF02D: "−",
    0xF03D: "=",
    0xF04C: "…",
    0xF0A2: "′",
    0xF0A3: "≤",
    0xF0AC: "←",
    0xF0B3: "≥",
    0xF0B4: "×",
    0xF0B8: "÷",
    0xF0B9: "≠",
    0xF0BB: "≈",
    0xF0CE: "∈",
    0xF0D6: "√",
    0xF0E5: "∑",
    0xF0F2: "∫",
}

# 成对出现的括号类符号：按行内出现顺序配成 ⌊ ⌋
BRACKETS = {0xF8F0, 0xF8FB}


def _fix_line(line: str) -> str:
    out = []
    opening = True
    for ch in line:
        code = ord(ch)
        if code in BRACKETS:
            out.append("⌊" if opening else "⌋")
            opening = not opening
        else:
            out.append(SYMBOLS.get(code, ch))
    return "".join(out)


def normalize_text(text: str) -> str:
    """按行规范化，保持行数不变（source_ref.line 仍然有效）。"""
    return "\n".join(_fix_line(ln) for ln in text.splitlines())


def has_unmapped(text: str) -> bool:
    return any(0xE000 <= ord(c) <= 0xF8FF for c in (text or ""))
