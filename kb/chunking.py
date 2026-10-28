# -*- coding: utf-8 -*-
"""文档切分：一段能逐字回溯到原文的连续文字 = 一个 chunk。

切分参数沿用 DeepTutor 的做法（`deeptutor/services/rag/pipelines/llamaindex/
ingestion.py` 里 SentenceSplitter 的 chunk_size=512 / chunk_overlap=50），
但这里用标准库实现，并且额外记录每个片段在原文里的起止位置：
引用校验仍然只认「片段是原文的子串」，前端也能据此跳回原句。
"""
import re
from typing import Dict, List

DEFAULT_TARGET = 512
DEFAULT_OVERLAP = 50

# 句子边界：中文句号/问号/叹号/分号、英文问叹号、换行。保留标点，切出来仍是原文。
_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?|\n+")


def _pieces(text: str, target: int) -> List[tuple]:
    """把长段落再切短，避免一整段没有标点的文本变成超大 chunk。"""
    out: List[tuple] = []
    for m in _SENT_RE.finditer(text):
        s, e = m.start(), m.end()
        while e - s > target * 2:
            out.append((s, s + target))
            s += target
        out.append((s, e))
    return out


def split_text(text: str, target: int = DEFAULT_TARGET,
               overlap: int = DEFAULT_OVERLAP) -> List[Dict]:
    """返回 [{seq, text, start, end}]；text 恰好等于原文 [start:end]。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    target = max(120, int(target))
    overlap = max(0, min(int(overlap), target // 2))
    pieces = _pieces(text, target)
    if not pieces:
        return []

    spans: List[tuple] = []
    cur_start = cur_end = None
    for s, e in pieces:
        if cur_start is None:
            cur_start, cur_end = s, e
            continue
        long_enough = (cur_end - cur_start) >= target // 2
        if long_enough and (e - cur_start) > target:
            spans.append((cur_start, cur_end))
            # 重叠部分从上一段末尾往前找，落在一个句子边界上
            back = max(cur_start, cur_end - overlap)
            ns = cur_end
            for ps, pe in pieces:
                if ps >= cur_end:
                    break
                if ps >= back and pe <= cur_end:
                    ns = ps
                    break
            cur_start, cur_end = min(ns, cur_end), e
        else:
            cur_end = e
    if cur_start is not None:
        spans.append((cur_start, cur_end))

    out: List[Dict] = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e - s < 10:
            continue
        out.append({"seq": len(out) + 1, "text": text[s:e], "start": s, "end": e})
    return out
