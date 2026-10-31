# -*- coding: utf-8 -*-
"""表格统计：统计类问题要读全表，数字由服务端逐行数出，不许模型估。"""
import json
import random
import unittest

from answer.aggregate import (TableStats, check_counts, is_agg_question,
                              is_count_question, is_list_question, merge_docs,
                              needs_full_doc, table_stats)
from answer.cache import cacheable
from answer.pipeline import AnswerPipeline
from kb.chunking import split_text
from retrieval.chunk import Chunk
from retrieval.search import SearchHit

HEADER = "学号 | 姓名 | 性别 | 学院 | 年级"
MALES = ["陈畅", "陈冬阳", "高哲", "郭轩赫", "侯煜恒", "郝凌恺", "李怡宝",
         "林志翔", "陆翔宇", "毛恒臣", "任昭旭", "孙正阳", "王玉川", "闫冠南",
         "杨政道", "王知博", "张文杰", "赵维彬"]
FEMALES = ["陈曦", "贾丽苑", "翟景华", "李倩颖", "路佳怡", "石佳姿",
           "孙源晨", "王茜玮", "于孟楠", "张熙雯", "张语桐", "赵晓灿"]


def roster_text():
    lines = ["工作表：Sheet1", HEADER]
    n = 1
    for who, sex in [(m, "男") for m in MALES] + [(f, "女") for f in FEMALES]:
        lines.append("%d | %s | %s | 软件学院 | 2023" % (542313331100 + n, who, sex))
        n += 1
    return "\n".join(lines)


def doc_chunks(text, title="智能科学与技术23-01班级名单"):
    chunks = []
    for part in split_text(text):
        chunks.append(Chunk(
            id="doc-1-%03d" % part["seq"], seq=part["seq"], text=part["text"],
            kind="doc", title=title, subject="个人知识库",
            source_ref={"document_id": "doc-1", "document_title": title,
                        "start": part["start"], "end": part["end"]}))
    return chunks


def roster_chunks(shuffle=False):
    chunks = doc_chunks(roster_text())
    if shuffle:
        random.Random(7).shuffle(chunks)
    return chunks


class CountQuestionTest(unittest.TestCase):
    def test_count_phrases(self):
        for q in ("智科2301班男生有多少女生有多少", "名单里一共几个人",
                  "软件学院占多少", "男女生比例是多少", "各科目分别考几题"):
            self.assertTrue(is_count_question(q), q)

    def test_normal_phrases(self):
        for q in ("进程和线程的区别", "二叉树的遍历方式", "什么是虚拟内存"):
            self.assertFalse(is_count_question(q), q)

    def test_other_full_doc_intents(self):
        for q in ("谁在求职", "名单里都有哪些人", "列出所有住在 3 号楼的",
                  "平均分是多少", "最高分是谁", "总结一下这份讲义",
                  "这份资料讲了什么"):
            self.assertTrue(needs_full_doc(q), q)
        for q in ("进程和线程的区别", "陈畅的性别", "什么是虚拟内存"):
            self.assertFalse(needs_full_doc(q), q)

    def test_intent_families_do_not_overlap_too_much(self):
        self.assertTrue(is_list_question("谁在求职"))
        self.assertFalse(is_list_question("平均分是多少"))
        self.assertTrue(is_agg_question("平均分是多少"))
        self.assertFalse(is_agg_question("进程和线程的区别"))


class MergeTest(unittest.TestCase):
    def test_overlap_is_removed_not_double_counted(self):
        chunks = roster_chunks()
        self.assertGreater(len(chunks), 1)
        (_, lines), = merge_docs(chunks)
        text = "\n".join(lines)
        self.assertEqual(text, roster_text())
        self.assertEqual(text.count("陈畅"), 1)

    def test_shuffled_chunks_merge_the_same(self):
        (_, lines), = merge_docs(roster_chunks(shuffle=True))
        self.assertEqual("\n".join(lines), roster_text())


class TableStatsTest(unittest.TestCase):
    def test_gender_counts_are_exact(self):
        stats = table_stats(roster_chunks(), "男生有多少女生有多少")
        self.assertIsNotNone(stats)
        self.assertIn("有效数据 30 行", stats.text)
        self.assertIn("男 18 行", stats.text)
        self.assertIn("女 12 行", stats.text)
        self.assertIn(30, stats.numbers)
        self.assertIn(18, stats.numbers)
        self.assertIn(12, stats.numbers)

    def test_headerless_table_does_not_lose_the_first_row(self):
        # 线上那份名单的 xlsx 没有表头：第一行就是陈畅，被当成表头吃掉后
        # 变成 29 行、男 17。第一行带长学号，必须按数据处理。
        text = "\n".join(["工作表：Sheet1"] + roster_text().splitlines()[2:])
        stats = table_stats(doc_chunks(text), "智科2301班男生有多少女生有多少")
        self.assertIn("有效数据 30 行", stats.text)
        self.assertIn("男 18 行", stats.text)
        self.assertIn("女 12 行", stats.text)
        self.assertNotIn("男 17 行", stats.text)
        self.assertTrue(check_counts("智科2301班男生17人，女生12人。", stats))

    def test_headerless_table_without_ids_is_still_data(self):
        text = "\n".join(["工作表：名单", "张三 | 男", "李四 | 女", "王五 | 男"])
        stats = table_stats(doc_chunks(text), "男生有多少")
        self.assertIn("有效数据 3 行", stats.text)
        self.assertIn("男 2 行", stats.text)
        self.assertIn("女 1 行", stats.text)

    def test_columns_query_does_not_mention_are_not_dumped(self):
        stats = table_stats(roster_chunks(), "男生有多少女生有多少")
        self.assertNotIn("学号」列", stats.text)
        self.assertNotIn("姓名」列", stats.text)

    def test_digit_cells_are_not_matched_inside_a_class_number(self):
        # 问「2301 班」时，床位 1、3 这些单个数字不能算「问题里点到」的列
        text = "\n".join(["工作表：Sheet1", "姓名 | 性别 | 床位",
                          "张三 | 男 | 1", "李四 | 女 | 3"])
        stats = table_stats(doc_chunks(text), "智科2301班男生有多少女生有多少")
        self.assertIn("男 1 行", stats.text)
        self.assertIn("女 1 行", stats.text)
        self.assertNotIn("床位", stats.text)

    def test_prose_only_doc_has_no_stats(self):
        text = "进程是资源分配的单位，线程是调度的基本单位。" * 20
        chunks = [Chunk(id="doc-1-001", seq=1, text=text, kind="doc",
                        source_ref={"document_id": "doc-1", "start": 0,
                                    "end": len(text)})]
        self.assertIsNone(table_stats(chunks, "有多少种"))

    def test_count_check_rejects_numbers_that_were_not_counted(self):
        stats = table_stats(roster_chunks(), "男生有多少女生有多少")
        self.assertEqual(check_counts("共列出 30 人，其中男生 18 人、女生 12 人。", stats), [])
        bad = check_counts("共列出 25 人，其中男生 12 人、女生 13 人。", stats)
        self.assertTrue(bad)
        self.assertIn("25人", bad[0].replace(" ", ""))

    def test_list_question_gets_the_matching_rows_not_just_counts(self):
        lines = ["工作表：就业登记", "学号 | 姓名 | 就业状态"]
        for i in range(1, 31):
            status = "求职" if i <= 3 else ("升学" if i <= 10 else "已就业")
            lines.append("5423133311%02d | 同学%02d | %s" % (i, i, status))
        stats = table_stats(doc_chunks("\n".join(lines), title="就业台账"),
                            "谁在求职")
        self.assertIn("匹配「求职」的行（共 3 行）", stats.text)
        self.assertIn("同学01 | 求职", stats.text)
        self.assertIn("同学03 | 求职", stats.text)
        self.assertNotIn("同学04 | 求职", stats.text)
        self.assertIn(3, stats.numbers)

    def test_numeric_columns_are_summed_and_averaged_on_the_server(self):
        lines = ["工作表：成绩", "姓名 | 分数",
                 "张三 | 90", "李四 | 80", "王五 | 100"]
        stats = table_stats(doc_chunks("\n".join(lines), title="成绩单"),
                            "平均分是多少")
        self.assertIn("「分数」列（3 个数值）", stats.text)
        self.assertIn("合计 270", stats.text)
        self.assertIn("平均 90", stats.text)
        self.assertIn("最高 100", stats.text)
        self.assertIn("最低 80", stats.text)

    def test_id_columns_are_not_averaged(self):
        stats = table_stats(roster_chunks(), "平均分是多少")
        # 名单里只有学号、年级这类编号列，不能拿来算平均
        self.assertNotIn("平均", stats.text)

    def test_count_check_is_off_without_table_stats(self):
        self.assertEqual(check_counts("共 25 人。", None), [])
        self.assertEqual(check_counts("共 25 人。", TableStats(text="只看了一部分")), [])

    def test_partial_scan_forbids_precise_numbers(self):
        stats = TableStats(text="资料没有读全", partial=True)
        self.assertTrue(check_counts("名单里一共有 30 人。", stats))
        self.assertEqual(check_counts("资料太多，只读到一部分，缩小范围再问。", stats), [])


class FakeSearcher:
    def __init__(self, chunks, top=5):
        self.chunks = list(chunks)
        self.top = top
        self.calls = 0

    def search(self, query, skip_rerank=False):
        self.calls += 1
        return [SearchHit(c, 1.0 if i == 0 else 0.5)
                for i, c in enumerate(self.chunks[:self.top])]


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, messages, hits=None):
        self.calls.append(messages)
        return self.responses.pop(0)


def payload(answer, quote="542313331101 | 陈畅 | 男"):
    return json.dumps({"refused": False, "answer": answer,
                       "citations": [{"chunk_id": "doc-1-001", "quote": quote}],
                       "reason": ""}, ensure_ascii=False)


class PipelineTest(unittest.TestCase):
    def test_count_question_reads_the_whole_table(self):
        chunks = roster_chunks(shuffle=True)
        # top-2 必然漏行；展开后应把 30 行都交给模型，召回条数仍按 2 显示
        provider = FakeProvider([payload("共列出 30 人，其中男生 18 人、女生 12 人。")])
        pipe = AnswerPipeline(FakeSearcher(chunks, top=2), provider,
                              prompt_mode="doc")
        res = pipe.answer("智科2301班男生有多少女生有多少")
        self.assertFalse(res.refused, res.reason)
        self.assertEqual(len(res.hits), 2)
        self.assertEqual(len(res.context), len(chunks))
        prompt = provider.calls[0][-1]["content"]
        self.assertIn("【表格统计】", prompt)
        self.assertIn("男 18 行", prompt)
        # 30 行全在 prompt 里
        self.assertIn(FEMALES[-1], prompt)

    def test_list_question_also_reads_the_whole_table(self):
        lines = ["工作表：就业登记", "学号 | 姓名 | 就业状态"]
        for i in range(1, 31):
            status = "求职" if i <= 3 else ("升学" if i <= 10 else "已就业")
            lines.append("5423133311%02d | 同学%02d | %s" % (i, i, status))
        chunks = doc_chunks("\n".join(lines), title="就业台账")
        quote = "542313331101 | 同学01 | 求职"
        provider = FakeProvider([payload(
            "目前有 3 人在求职：同学01、同学02、同学03。", quote=quote)])
        pipe = AnswerPipeline(FakeSearcher(chunks, top=1), provider,
                              prompt_mode="doc")
        res = pipe.answer("谁在求职")
        self.assertFalse(res.refused, res.reason)
        self.assertEqual(len(res.hits), 1)
        self.assertEqual(len(res.context), len(chunks))
        prompt = provider.calls[0][-1]["content"]
        self.assertIn("匹配「求职」的行（共 3 行）", prompt)
        self.assertIn("同学03", prompt)
        self.assertIn("同学30", prompt)

    def test_ordinary_question_gets_neighbor_chunks_only(self):
        chunks = []
        for i in range(4):
            text = "第 %d 段：" % (i + 1) + "讲" * 100
            chunks.append(Chunk(
                id="doc-1-%03d" % (i + 1), seq=i + 1, text=text, kind="doc",
                title="讲义",
                source_ref={"document_id": "doc-1", "document_title": "讲义",
                            "start": i * 120, "end": i * 120 + len(text)}))
        hits = [SearchHit(chunks[1], 1.0)]
        pipe = AnswerPipeline(FakeSearcher(chunks, top=1), FakeProvider([]),
                              prompt_mode="doc")
        context = pipe._with_neighbors(hits, chunks)
        self.assertEqual([h.chunk.id for h in context],
                         ["doc-1-002", "doc-1-001", "doc-1-003"])
        self.assertNotIn("doc-1-004", [h.chunk.id for h in context])

    def test_ordinary_question_does_not_read_the_whole_doc(self):
        chunks = roster_chunks()
        provider = FakeProvider([payload("陈畅是男生。")])
        pipe = AnswerPipeline(FakeSearcher(chunks, top=1), provider,
                              prompt_mode="doc")
        res = pipe.answer("陈畅的性别")
        self.assertFalse(res.refused, res.reason)
        self.assertEqual(len(res.hits), 1)
        # 补邻居不等于读全库；问题里没有说话要读全表
        self.assertLess(len(res.context), len(chunks))
        self.assertNotIn("【表格统计】", provider.calls[0][-1]["content"])

    def test_wrong_numbers_are_retried_then_refused(self):
        chunks = roster_chunks()
        provider = FakeProvider([
            payload("共列出 25 人，其中男生 12 人、女生 13 人。"),
            payload("共列出 25 人，其中男生 12 人、女生 13 人。"),
        ])
        pipe = AnswerPipeline(FakeSearcher(chunks), provider, prompt_mode="doc")
        res = pipe.answer("智科2301班男生有多少女生有多少")
        self.assertTrue(res.refused)
        self.assertTrue(res.reason.startswith("stat_failed"), res.reason)
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(any("【表格统计】" in m["content"]
                            for m in provider.calls[1]))

    def test_stats_only_count_the_document_that_matched(self):
        # 同库里另一张表也有「男 25 行」：它没被召回，就不能进允许值
        roster = roster_chunks()
        other_text = "\n".join(
            ["工作表：Sheet2", "姓名 | 性别"]
            + ["某人 %d | 男" % i for i in range(25)])
        other = []
        for part in split_text(other_text):
            other.append(Chunk(
                id="doc-2-%03d" % part["seq"], seq=part["seq"],
                text=part["text"], kind="doc", title="另一张表",
                subject="个人知识库",
                source_ref={"document_id": "doc-2", "document_title": "另一张表",
                            "start": part["start"], "end": part["end"]}))
        # 只让名单本身被召回；另一张表在库里，但这一问没有命中它
        searcher = FakeSearcher(roster + other, top=len(roster))
        pipe = AnswerPipeline(searcher, FakeProvider([]), prompt_mode="doc")
        context, stats = pipe._prepare("男生有多少女生有多少",
                                       searcher.search("q"))
        self.assertIn(18, stats.numbers)
        self.assertNotIn(25, stats.numbers)
        self.assertNotIn("另一张表", stats.text)
        self.assertEqual(len(context), len(roster) + len(other))

    def test_prose_question_is_not_expanded(self):
        chunks = roster_chunks()
        provider = FakeProvider([payload("陈畅是男生。")])
        pipe = AnswerPipeline(FakeSearcher(chunks), provider, prompt_mode="doc")
        res = pipe.answer("陈畅的性别")
        self.assertFalse(res.refused, res.reason)
        self.assertEqual(len(res.hits), min(5, len(chunks)))
        self.assertNotIn("【表格统计】", provider.calls[0][-1]["content"])

    def test_oversized_library_tells_the_model_it_is_partial(self):
        big = [Chunk(id="doc-1-%03d" % i, seq=i,
                     text=("第 %d 段：" % i) + "讲" * 1500, kind="doc",
                     source_ref={"document_id": "doc-1", "start": i * 1600,
                                 "end": i * 1600 + 1509})
               for i in range(1, 31)]
        quote = "第 1 段：讲讲讲讲讲讲"
        provider = FakeProvider([
            payload("名单里一共有 30 人。", quote=quote),
            payload("资料没有读全，只看到一部分，请缩小范围再问。", quote=quote),
        ])
        pipe = AnswerPipeline(FakeSearcher(big, top=5), provider, prompt_mode="doc")
        res = pipe.answer("名单里男生有多少人")
        # 第一次给了精确数字 → 退回重答；第二次承认没读全，才放行
        self.assertFalse(res.refused, res.reason)
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("没有读全", provider.calls[0][-1]["content"])

    def test_exam_mode_is_untouched(self):
        chunks = roster_chunks()
        provider = FakeProvider([payload("陈畅是男生。")])
        pipe = AnswerPipeline(FakeSearcher(chunks), provider, prompt_mode="exam")
        res = pipe.answer("智科2301班男生有多少")
        self.assertEqual(len(res.hits), min(5, len(chunks)))


class CacheTest(unittest.TestCase):
    def test_full_doc_questions_are_not_cached_for_user_libraries(self):
        for q in ("男生有多少女生有多少", "谁在求职", "平均分是多少",
                  "总结一下这份讲义"):
            self.assertFalse(cacheable(q, preset=False), q)
        self.assertTrue(cacheable("进程和线程的区别", preset=False))
        # 内置语料是静态的，照常缓存
        self.assertTrue(cacheable("男生有多少女生有多少", preset=True))


if __name__ == "__main__":
    unittest.main()
