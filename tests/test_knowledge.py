# -*- coding: utf-8 -*-
"""考点卡片测试：加载、校验、筛选和题目关联。"""
import json
import os
import tempfile
import unittest

from api.knowledge import (filter_knowledge, know_brief, know_detail,
                           knowledge_chapters, knowledge_for_question,
                           knowledge_subject_counts, load_knowledge,
                           related_knowledge_by_question)


def card(cid="ds-quicksort", subject="ds", chapter="排序", **kw):
    row = {
        "id": cid,
        "subject": subject,
        "chapter": chapter,
        "title": kw.pop("title", "快速排序"),
        "summary": kw.pop("summary", "平均 O(n log n)"),
        "body": kw.pop("body", "快速排序平均时间复杂度 O(n log n)。"),
        "formulas": kw.pop("formulas", ["最坏 T(n)=O(n²)"]),
        "pitfalls": kw.pop("pitfalls", "快速排序不稳定。"),
        "related_question_ids": kw.pop("related_question_ids", ["2019-408-10"]),
        "source": "self",
        "status": "draft",
    }
    row.update(kw)
    return row


class LoadTest(unittest.TestCase):
    def _write(self, rows):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "knowledge.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def test_loads_and_attaches_source_line(self):
        path = self._write([card(), card("os-lru", "os", "内存管理", title="LRU")])
        cards = load_knowledge(path, "knowledge.jsonl")
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["_line"], 1)
        self.assertEqual(cards[0]["_file"], "knowledge.jsonl")
        self.assertEqual(cards[1]["subject"], "os")

    def test_later_duplicate_wins(self):
        path = self._write([card(title="旧标题"), card(title="新标题")])
        cards = load_knowledge(path)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "新标题")

    def test_missing_required_field(self):
        bad = card()
        del bad["body"]
        with self.assertRaises(ValueError) as ctx:
            load_knowledge(self._write([bad]))
        self.assertIn("body", str(ctx.exception))

    def test_unknown_subject_rejected(self):
        with self.assertRaises(ValueError):
            load_knowledge(self._write([card(subject="math")]))

    def test_blank_lines_ignored(self):
        path = self._write([card()])
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\n")
        self.assertEqual(len(load_knowledge(path)), 1)


class SerializeTest(unittest.TestCase):
    def test_brief_has_no_body(self):
        item = know_brief(card())
        self.assertEqual(item["title"], "快速排序")
        self.assertEqual(item["related_count"], 1)
        self.assertNotIn("body", item)
        self.assertNotIn("_line", item)

    def test_detail_links_questions(self):
        from api.catalog import detail

        q = {"id": "2019-408-10", "seq": 10, "stem": "快速排序题",
             "year": 2019, "subject": "ds", "options": {"A": "甲"}}
        item = know_detail(card(related_question_ids=["2019-408-10", "2019-408-99"]),
                           {"2019-408-10": q})
        self.assertEqual(item["related_questions"][0]["id"], "2019-408-10")
        self.assertEqual(item["missing_questions"], ["2019-408-99"])
        self.assertEqual(item["source_ref"]["line"], 0)
        self.assertIn("非教材原文", item["provenance"])
        # 确保没有把私有字段带出去
        self.assertNotIn("_file", item)

    def test_detail_question_brief_has_no_answer(self):
        from api.catalog import detail

        q = {"id": "2019-408-10", "seq": 10, "stem": "题", "year": 2019,
             "subject": "ds", "answer": "A"}
        item = know_detail(card(), {"2019-408-10": q})
        self.assertNotIn("answer", item["related_questions"][0])
        self.assertEqual(detail(q)["answer"], "A")


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.cards = [
            card("ds-quicksort", "ds", "排序", title="快速排序"),
            card("ds-hash", "ds", "查找", title="散列表",
                 body="线性探查解决冲突。", summary="线性探查",
                 pitfalls="删除不能直接置空。"),
            card("os-lru", "os", "内存管理", title="LRU 页面置换",
                 summary="淘汰最久未使用页", body="LRU 淘汰最久未使用的页。",
                 pitfalls="缺页不一定发生置换。"),
        ]

    def test_filter_by_subject_and_chapter(self):
        total, rows = filter_knowledge(self.cards, subject="ds")
        self.assertEqual(total, 2)
        total, rows = filter_knowledge(self.cards, subject="ds", chapter="排序")
        self.assertEqual([r["id"] for r in rows], ["ds-quicksort"])

    def test_search_hits_body_and_pitfalls(self):
        _, rows = filter_knowledge(self.cards, query="线性探查")
        self.assertEqual([r["id"] for r in rows], ["ds-hash"])
        _, rows = filter_knowledge(self.cards, query="不稳定")
        self.assertEqual([r["id"] for r in rows], ["ds-quicksort"])

    def test_pagination(self):
        total, rows = filter_knowledge(self.cards, limit=1, offset=1)
        self.assertEqual(total, 3)
        self.assertEqual(rows[0]["id"], "ds-quicksort")

    def test_subject_counts_keep_zero_subjects(self):
        counts = knowledge_subject_counts([self.cards[0]])
        self.assertEqual([c["key"] for c in counts], ["ds", "co", "os", "cn"])
        self.assertEqual(counts[1]["count"], 0)

    def test_chapters_only_for_selected_subject(self):
        chaps = knowledge_chapters(self.cards, "ds")
        self.assertEqual([c["name"] for c in chaps], ["查找", "排序"])


class QuestionLinkTest(unittest.TestCase):
    def test_question_to_cards(self):
        cards = [card(), card("ds-sorting", "ds", "排序", title="排序稳定性",
                              related_question_ids=["2019-408-10"])]
        linked = knowledge_for_question(cards, "2019-408-10")
        self.assertEqual(len(linked), 2)
        self.assertEqual(linked[0]["id"], "ds-quicksort")

    def test_reverse_index(self):
        idx = related_knowledge_by_question([card(), card("os-lru", "os", "内存管理",
                                                         related_question_ids=["2019-408-29"])])
        self.assertIn("2019-408-10", idx)
        self.assertIn("2019-408-29", idx)
        self.assertEqual(idx["2019-408-10"][0]["title"], "快速排序")


if __name__ == "__main__":
    unittest.main()
