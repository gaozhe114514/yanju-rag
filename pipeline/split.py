# -*- coding: utf-8 -*-
"""把整份试卷/答案文本，按题号切成单题块。规则优先，不花 token。"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

NUM = re.compile(r'^\s*(\d{1,2})\s*[\.．、]\s*')
RANGE = re.compile(r'^\s*(\d{1,2})\s*[-~—]\s*(\d{1,2})\s+([A-D\s]+)$')
SECTION = re.compile(r'^\s*[一二三四五六七八九十]+\s*、')
ANSWER_MARKER = re.compile(r'【答案】|参考答案|答案\s*[:：]')


@dataclass
class Block:
    seq: int
    text: str
    start_line: int
    warnings: List[str] = field(default_factory=list)


def split_numbered(text: str, max_seq: int = 47,
                   require_marker: bool = False) -> Tuple[List[Block], List[str]]:
    """按 'N.' / 'N、' 切块。只接受递增且从 1 开始的题号，避免把选项里的编号当成新题。

    require_marker=True 时，候选题号行必须带【答案】标记，
    否则视为解析里的步骤编号（如 "3. 首先…"），不切新题。
    """
    lines = text.splitlines()
    blocks: List[Block] = []
    warns: List[str] = []
    cur: Block | None = None
    last = 0
    for i, ln in enumerate(lines):
        m = NUM.match(ln)
        ok, n = False, 0
        if m:
            n = int(m.group(1))
            if 1 <= n <= max_seq:
                if last == 0 and n == 1:
                    ok = True
                elif 1 <= last < n <= max_seq:
                    ok = True
                if ok and require_marker and not ANSWER_MARKER.search(ln):
                    ok = False
                if ok and last and n != last + 1:
                    warns.append("题号跳跃: %d -> %d（第 %d 行）" % (last, n, i + 1))
        # 大题标题（"三、综合应用题"）是天然边界，别让它混进上一题的选项里
        if not ok and cur is not None and SECTION.match(ln):
            blocks.append(cur)
            cur = None
            continue
        if ok:
            if cur is not None:
                blocks.append(cur)
            cur = Block(seq=n, text=ln[m.end():].strip(), start_line=i + 1)
            last = n
        elif cur is not None:
            cur.text += "\n" + ln
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        b.text = b.text.strip()
    if not blocks:
        warns.append("未识别到任何题号：题号需形如 1. / 1、 且从 1 开始递增")
    return blocks, warns


def parse_answer_table(text: str) -> Dict[int, str]:
    """兜底：识别 '1-5 BCADA' 这种答案速查表。"""
    res: Dict[int, str] = {}
    for ln in text.splitlines():
        m = RANGE.match(ln.strip())
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        letters = re.sub(r'\s+', '', m.group(3))
        if len(letters) == b - a + 1:
            for k, ch in enumerate(letters):
                res[a + k] = ch
    return res


def split_answers(text: str, max_seq: int = 47) -> Tuple[Dict[int, str], List[str]]:
    """答案与解析：优先按题号分块（块内含【答案】【解析】），否则退回速查表。"""
    blocks, warns = split_numbered(text, max_seq=max_seq, require_marker=True)
    if not blocks:
        # 兼容不带【答案】标记的格式：退回普通切分，但要提示人工注意
        blocks, w2 = split_numbered(text, max_seq=max_seq)
        warns.extend(w2)
        if blocks:
            warns.append("答案未带【答案】标记，按普通题号切分，注意核对是否误切解析步骤")
    ans: Dict[int, str] = {}
    for b in blocks:
        ans[b.seq] = b.text
    if not ans:
        ans = parse_answer_table(text)
        if ans:
            warns.append("使用答案速查表兜底：仅得到选项字母，没有解析")
    return ans, warns
