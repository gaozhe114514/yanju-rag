# -*- coding: utf-8 -*-
"""轻量中英混合分词：中文按字符二元组，英文/数字按词。

不引入 jieba，是为了让检索层零依赖、可测试；
二元组对「一字之差」密集的 408 术语（根本/基本、中断/异常）反而更稳。
"""
import re
from typing import List

CJK = re.compile(r'[一-鿿]+')
WORD = re.compile(r'[A-Za-z0-9_]+')

# 高频但不具区分度的二元组
STOP = {
    "的是", "下列", "关于", "中正", "正确", "错误", "一个", "可以",
    "需要", "进行", "叙述", "说法", "选项", "如下", "所示",
}


def tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    out: List[str] = []
    for m in CJK.finditer(text):
        s = m.group()
        if len(s) == 1:
            out.append(s)
        else:
            for i in range(len(s) - 1):
                bg = s[i:i + 2]
                if bg not in STOP:
                    out.append(bg)
    for m in WORD.finditer(text):
        w = m.group()
        if len(w) > 1 or w.isdigit():
            out.append(w)
    return out
