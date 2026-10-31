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
# 列举类问法：要的不是一个数字，而是完整的名单。只给 top-5 片段同样会漏人。
_LIST_RE = re.compile(r"谁|哪些|列出|罗列|名单|清单|都有|分别是|各是|逐个")
# 汇总类问法：平均、极值、求和。模型心算同样不可靠。
_AGG_RE = re.compile(r"平均|均值|最高|最低|最大|最小|合计|总和|求和|总计")
# 概括类问法：总结、目录、章节。只读几段就总结，结论必然缺一块。
_SUMMARY_RE = re.compile(r"总结|概括|摘要|讲了什么|讲什么|主要内容|大意|目录|章节|大纲")

# 一次把所有片段读进上下文的上限。超过就退回 top-5，并声明「没有读全」。
MAX_SCAN_CHARS = 30000
# 一个分类列最多列出几个取值；学号、姓名这种唯一值不逐个列。
MAX_VALUES = 12
MAX_VALUE_LEN = 20
# 一张表最多报几列、整个库最多报几张表，避免统计块喧宾夺主
MAX_COLUMNS = 4
MAX_TABLES = 5
# 列举类问题最多把多少行原样列给模型；再多就说明「只列前 N 行」
MAX_LIST_ROWS = 30

PARTIAL_NOTE = (
    "这份资料一共 %d 字，超过一次能通读的 %d 字上限，本次没有读全，只拿到其中一部分。"
    "涉及统计、合计、总数、完整名单或全文总结的问题不要给出精确数字或完整清单，"
    "先说明只看到一部分，并请用户缩小范围。"
)


def is_count_question(query: str) -> bool:
    return bool(_COUNT_RE.search(query or ""))


def is_list_question(query: str) -> bool:
    return bool(_LIST_RE.search(query or ""))


def is_agg_question(query: str) -> bool:
    return bool(_AGG_RE.search(query or ""))


def needs_full_doc(query: str) -> bool:
    """这个问题是不是必须读全整份资料才能答对。

    数量、名单、平均/极值、全文总结都算：只给 top-5 片段，答案会缺行或缺段，
    而且看起来仍然像模像样。普通问答不需要读全，交给检索与精排即可。
    """
    q = query or ""
    return bool(_COUNT_RE.search(q) or _LIST_RE.search(q)
                or _AGG_RE.search(q) or _SUMMARY_RE.search(q))


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


# 编号类列不做平均/求和：学号、床位、宿舍号加起来没有意义
_ID_NAME_RE = re.compile(r"学号|编号|号码|电话|手机|身份证|id$|宿舍号|床位|年份|年级",
                         re.I)


def _to_num(value: str) -> Optional[float]:
    text = (value or "").strip().replace(",", "").rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.1f" % value).rstrip("0").rstrip(".")


def _name_in_query(name: str, query: str) -> bool:
    if not name:
        return False
    if name in query:
        return True
    # 「平均分」对不上「分数」这种整名包含，退到两字片段
    return any(name[i:i + 2] in query for i in range(len(name) - 1))


def _aggregate_line(name: str, values) -> str:
    total = sum(values)
    line = ("「%s」列（%d 个数值）：合计 %s、平均 %s、最高 %s、最低 %s"
            % (name or "未命名列", len(values), _fmt_num(total),
               _fmt_num(total / len(values)), _fmt_num(max(values)),
               _fmt_num(min(values))))
    return "- " + line


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
            matched_values = set()
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
                matched_values.update(matched)
                reported += 1

            # 问「谁 / 哪些 / 列出」时，只给列的分布仍然要模型自己回表找人。
            # 把命中的行原样列出来，名字与列值始终挨在一起。
            if matched_values and _LIST_RE.search(q):
                rows = [row for row in data
                        if any(cell in matched_values for cell in row)]
                if rows:
                    lines_out.append("- 匹配「%s」的行（共 %d 行）"
                                     % ("、".join(sorted(matched_values)),
                                        len(rows)))
                    numbers.append(len(rows))
                    for row in rows[:MAX_LIST_ROWS]:
                        lines_out.append("  - " + " | ".join(row))
                    if len(rows) > MAX_LIST_ROWS:
                        lines_out.append("  - 只列前 %d 行；完整清单请缩小范围再问。"
                                         % MAX_LIST_ROWS)

            # 问平均 / 最高 / 最低 / 合计时，由服务端算，不让模型心算。
            if _AGG_RE.search(q):
                numeric = []
                for i, name in enumerate(header):
                    if _ID_NAME_RE.search(name or ""):
                        continue
                    values = [_to_num(row[i]) for row in data if i < len(row)]
                    values = [v for v in values if v is not None]
                    filled = sum(1 for row in data if i < len(row) and row[i])
                    if len(values) >= 3 and filled and len(values) >= filled * 0.8:
                        numeric.append((name, values))
                picked = [c for c in numeric if _name_in_query(c[0], q)]
                if not picked and len(numeric) == 1:
                    picked = numeric
                for name, values in picked[:1]:
                    lines_out.append(_aggregate_line(name, values))

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
