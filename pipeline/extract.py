# -*- coding: utf-8 -*-
"""单题块 -> 结构化 JSON。provider 可插拔：mock（离线自测）/ deepseek。"""
import json
import os
import re
import time
import urllib.request
from typing import Any, Dict

from .prompts import SYSTEM, USER

OPT = re.compile(r'([A-D])\s*[\.．、]')
ANS = re.compile(r'(?:【答案】|答案\s*[:：])\s*([A-D])')
ANALYSIS = re.compile(r'(?:【解析】|解析\s*[:：])\s*')

SUBJECT_HINTS = [
    ("ds", ["二叉树", "排序", "哈希", "栈", "队列", "图", "邻接", "KMP", "B树", "复杂度"]),
    ("co", ["Cache", "cache", "流水线", "指令", "总线", "主存", "浮点", "补码", "DMA", "中断向量"]),
    ("os", ["进程", "页表", "TLB", "调度", "信号量", "死锁", "文件系统", "虚拟内存", "缺页"]),
    ("cn", ["TCP", "UDP", "IP", "路由", "以太网", "拥塞", "子网", "DNS", "CSMA"]),
]

# 408 试卷的单选题分区多年稳定，按题号定科比关键词猜测可靠（关键词会把「位图」
# 里的「图」算成数据结构，也会漏掉没有术语的题）。
SECTION_SUBJECTS = (
    (1, 11, "ds"), (12, 22, "co"), (23, 32, "os"), (33, 40, "cn"),
)

# 综合题的四科顺序逐年在变：2019 是 DS/OS/CO/CN，2020 是 DS/CO/OS/CN。
# 没有登记年份时返回 None，交给 provider 的关键词判断兜底。
COMPREHENSIVE_SUBJECTS = {
    2019: ("ds", "ds", "os", "os", "co", "co", "cn"),
    2020: ("ds", "ds", "co", "co", "os", "os", "cn"),
    2023: ("ds", "ds", "co", "co", "os", "os", "cn"),
}


def subject_for_seq(seq: int, year: int = None):
    for lo, hi, subj in SECTION_SUBJECTS:
        if lo <= seq <= hi:
            return subj
    table = COMPREHENSIVE_SUBJECTS.get(year)
    if table and 41 <= seq <= 47:
        return table[seq - 41]
    return None


def _split_options(qtext: str):
    hits = []
    for m in OPT.finditer(qtext):
        ch = m.group(1)
        if not hits and ch == "A":
            hits.append(m)
        elif hits and ch == chr(ord(hits[-1].group(1)) + 1):
            hits.append(m)
    if len(hits) < 4:
        return qtext.strip(), {}
    stem = qtext[:hits[0].start()].strip()
    opts = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(qtext)
        opts[m.group(1)] = qtext[m.end():end].strip().replace("\n", " ")
    return stem, opts


def _guess_subject(text: str):
    best, score = None, 0
    for subj, kws in SUBJECT_HINTS:
        s = sum(text.count(k) for k in kws)
        if s > score:
            best, score = subj, s
    return best if score > 0 else None


def mock_extract(seq: int, qtext: str, atext: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """离线可跑的兜底实现：正则能抽多少算多少，抽不到的留空，让校验器暴露出来。"""
    stem, opts = _split_options(qtext)
    m = ANS.search(atext or "")
    answer = m.group(1) if m else ""
    analysis = ""
    ma = ANALYSIS.search(atext or "")
    if ma:
        analysis = (atext or "")[ma.end():].strip()
    subtype = "single" if seq <= 40 else "comprehensive"
    if subtype == "comprehensive":
        opts = {}
        answer = ""
    score = 2 if subtype == "single" else None
    # 真题里既有「本题 13 分」，也有「（13 分）」
    ms = re.search(r'本题\s*(\d+)\s*分|[（(]\s*(\d+)\s*分\s*[）)]', qtext)
    if subtype == "comprehensive" and ms:
        score = int(ms.group(1) or ms.group(2))
    return {
        "seq": seq,
        "type": subtype,
        "subject": subject_for_seq(seq, meta.get("year")) or _guess_subject(
            qtext + " " + (atext or "")),
        "score": score,
        "stem": stem,
        "options": opts,
        "answer": answer,
        "analysis": analysis,
        "kp_ids": [],
        "figures": [],
    }


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base: str = "https://api.deepseek.com/chat/completions"):
        self.api_key, self.model, self.base = api_key, model, base

    def extract(self, seq: int, qtext: str, atext: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": USER.format(question=qtext,
                                                        answer=atext or "（无）")},
            ],
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        })
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                raw = payload["choices"][0]["message"]["content"]
                out = json.loads(raw)
                out.setdefault("seq", seq)
                out.setdefault("kp_ids", [])
                out.setdefault("figures", [])
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("抽取失败 seq=%s: %s" % (seq, last))


def get_provider(name: str):
    if name == "mock":
        return type("Mock", (), {"name": "mock", "extract": staticmethod(mock_extract)})()
    if name == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise SystemExit("缺少环境变量 DEEPSEEK_API_KEY；或改用 --provider mock")
        return DeepSeekProvider(key)
    raise SystemExit("未知 provider: %s" % name)
