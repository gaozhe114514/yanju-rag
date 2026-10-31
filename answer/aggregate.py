# -*- coding: utf-8 -*-
"""表格与统计类问题：把整篇资料读全，由服务端逐行数，不让模型凭片段估。

问「名单里男生多少、女生多少」时，top-5 片段常常只覆盖一部分行：模型看到
的是一张缺了行的表，报出来的总数看着像模像样，其实是错的。这里加两层保护：

1. 统计类问法把同一个库的片段全部交给模型（总量超过上限就退回 top-5，
   并明确告诉模型「只看了一部分」，不许报精确总数）；
2. 服务端把同一文档的片段按原文位置接回去，按列逐行数出分布，写进 prompt；
   模型只负责组织语言，数字必须照抄。答案里的「N 人 / N 名」若与统计对不上，
   按引用校验同样的方式重试一次，仍不符就拒答。

统计只来自【资料】里的表格行，不另找知识，也没有写死的答案。
"""
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from retrieval.chunk import Chunk

# 统计类问法：问数量、合计、分布。只用于用户自建资料库（doc 模式）。
_COUNT_RE = re.compile(
    r"多少|几个|几人|几男|几女|几位|几名|几门|几所|几条|几段"
    r"|共有|一共|总共|合计|总计|统计|分别|人数|数量|比例|占比")

# 一次把所有片段读进上下文的上限。超过就退回 top-5，并声明「没有读全」。
MAX_SCAN_CHARS = 30000
# 一个分类列最多列出几个取值；学号、姓名这种唯一值不逐个列。
MAX_VALUES = 12
MAX_VALUE_LEN = 20
# 一张表最多报几列、整个库最多报几张表，避免统计块喧宾夺主
MAX_COLUMNS = 4
MAX_TABLES = 5

PARTIAL_NOTE = (
    "这份资料一共 %d 字，超过一次能通读的 %d 字上限，本次只检索到其中一部分片段。"
    "涉及统计、合计、总数的问题不要给出精确数字，先说明只看了一部分，并请用户缩小范围。"
)


def is_count_question(query: str) -> bool:
    return bool(_COUNT_RE.search(query or ""))


@dataclass
class TableStats:
    """给模型的统计说明，以及答案里允许出现的数字。

    partial=True 表示资料没有读全（超出一次能通读的上限），这时不许出现任何
    「N 人 / N 名」——数的是残缺的表，给出的精确数字一定是错的。
    """

    text: str
    numbers: List[int] = field(default_factory=list)
    partial: bool = False


def merge_docs(chunks: Sequence[Chunk]) -> List[Tuple[str, List[str]]]:
    """把同一文档的片段按原文位置接回成行，重叠部分只留一份。

    片段 text 恰好等于原文 [start:end]，所以行首的绝对偏移可以精确算出；
    重叠片段里的同一行偏移相同，用偏移去重，不会多算，也不会把表格里
    两行内容相同的合法数据合掉。片段没带位置信息时按 seq 顺序排列，不合并。
    """
    docs = OrderedDict()
    unknown_base = 10 ** 8
    for c in chunks:
        ref = c.source_ref or {}
        key = ref.get("document_id") or c.title or c.id
        entry = docs.setdefault(key, {"title": "", "lines": {}})
        if not entry["title"]:
            entry["title"] = ref.get("document_title") or c.title or ""
        start = ref.get("start")
        if start is None:
            start = unknown_base
            unknown_base += 10 ** 5
        pos = int(start)
        for raw in (c.text or "").splitlines(True):
            entry["lines"].setdefault(pos, raw.rstrip("\r\n"))
            pos += len(raw)

    out: List[Tuple[str, List[str]]] = []
    for entry in docs.values():
        lines = [entry["lines"][k] for k in sorted(entry["lines"])]
        out.append((entry["title"], lines))
    return out


_SEP = re.compile(r"\s*\|\s*")


def _tables(lines: Sequence[str]) -> List[dict]:
    """把表格行按「工作表：」分段；每张表的第一行是表头。"""
    tables: List[dict] = []
    current = None
    for raw in lines:
        line = (raw or "").strip()
        if line.startswith("工作表："):
            current = {"name": line[len("工作表："):].strip(), "rows": []}
            tables.append(current)
            continue
        if "|" not in line:
            continue
        cells = [c.strip() for c in _SEP.split(line)]
        if len(cells) < 2:
            continue
        if current is None:
            current = {"name": "", "rows": []}
            tables.append(current)
        current["rows"].append(cells)
    return [t for t in tables if len(t["rows"]) >= 2]


def _matches(value: str, query: str) -> bool:
    """问题里点到某个取值了吗。

    纯数字要按完整数字匹配：问「2301 班」时不能把床位「1」「3」也算进去，
    那是噪声列，还会把统计数字搅乱。中文值按子串匹配，「男」能对上「男生」。
    """
    if not value or len(value) > MAX_VALUE_LEN:
        return False
    if value.isdigit():
        if len(value) < 2:
            return False
        return re.search(r"(?<!\d)%s(?!\d)" % re.escape(value), query) is not None
    return value in query


def _column_text(name: str, values) -> str:
    name = name or "未命名列"
    pairs = "、".join("%s %d 行" % (v, n) for v, n in values)
    return "「%s」列：%s" % (name, pairs)


def table_stats(chunks: Sequence[Chunk], query: str) -> Optional[TableStats]:
    """按原文逐行数出表格分布；资料里没有表格时返回 None。"""
    q = query or ""
    blocks: List[str] = []
    numbers: List[int] = []
    for title, lines in merge_docs(chunks):
        for sheet in _tables(lines)[:MAX_TABLES]:
            header, data = sheet["rows"][0], sheet["rows"][1:]
            if not data:
                continue
            label = "《%s》" % (title or "资料")
            if sheet["name"]:
                label += "（工作表：%s）" % sheet["name"]
            lines_out = ["%s：有效数据 %d 行（不含表头）" % (label, len(data))]
            numbers.append(len(data))

            reported = 0
            for i, name in enumerate(header):
                if reported >= MAX_COLUMNS:
                    break
                values = [row[i] for row in data if i < len(row)]
                counts = Counter(v for v in values if v)
                if not counts:
                    continue
                matched = [v for v in counts if _matches(v, q)]
                if not matched:
                    continue
                # 小分类列（性别、状态这种）整列都报，问「男生多少」时也能看到女生；
                # 取值很多的列只报问题里点到的那几个，免得把整张表抄一遍。
                if len(counts) <= MAX_VALUES:
                    chosen = counts.most_common()
                else:
                    chosen = sorted(((v, counts[v]) for v in matched),
                                    key=lambda p: -p[1])
                lines_out.append("- " + _column_text(name, chosen))
                numbers.extend(n for _, n in chosen)
                reported += 1

            blocks.append("\n".join(lines_out))
    if not blocks:
        return None
    return TableStats(text="\n".join(blocks), numbers=numbers)


_COUNTED_RE = re.compile(r"(\d+)\s*(?:人|名|位)")


def check_counts(answer: str, stats: Optional[TableStats]) -> List[str]:
    """答案里的「N 人 / N 名」必须站得住：要么在表格统计里，要么资料没读全时不许给。"""
    if stats is None:
        return []
    found = [m.group(0).replace(" ", "")
             for m in _COUNTED_RE.finditer(answer or "") if int(m.group(1)) > 1]
    if stats.partial:
        if not found:
            return []
        return ["资料没有读全，不能给出「%s」这样的精确数字。"
                "请说明只看到了一部分，并请用户缩小范围，或把 refused 设为 true。"
                % "、".join(found[:3])]
    if not stats.numbers:
        return []
    allowed = set(int(n) for n in stats.numbers)
    bad = [m.group(0).replace(" ", "")
           for m in _COUNTED_RE.finditer(answer or "")
           if int(m.group(1)) > 1 and int(m.group(1)) not in allowed]
    if not bad:
        return []
    detail = stats.text.replace("\n", "；")
    if len(detail) > 200:
        detail = detail[:200] + "…"
    return ["回答里的「%s」和表格逐行数出的结果对不上。统计是：%s"
            " 请只用统计里的数字，或把 refused 设为 true。"
            % ("、".join(bad[:3]), detail)]
