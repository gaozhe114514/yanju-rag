# -*- coding: utf-8 -*-
"""回答反馈的归一化与 JSONL 统计测试：不需要 PostgreSQL。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from api.feedback import (feedback_stats, log_feedback, normalize_feedback,
                          reason_label)


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


class NormalizeTest(unittest.TestCase):
    def test_helpful_clears_reason(self):
        rec = normalize_feedback("  快速排序  ", True, "wrong_answer")
        self.assertEqual(rec["query"], "快速排序")
        self.assertEqual(rec["reason"], "")
        self.assertEqual(rec["citations"], [])

    def test_unknown_reason_falls_back(self):
        rec = normalize_feedback("LRU 缺页", False, "瞎写的")
        self.assertEqual(rec["reason"], "other")
        self.assertEqual(reason_label(rec["reason"]), "其他")

    def test_reason_labels(self):
        self.assertEqual(reason_label("wrong_answer"), "答非所问")
        self.assertEqual(reason_label("bad_citation"), "引用不对")
        self.assertEqual(reason_label("should_answer"), "资料里有但没答")
        self.assertEqual(reason_label(None), "其他")

    def test_citations_dedup_and_clip(self):
        rec = normalize_feedback("q", False, "bad_citation",
                                 citations=["a", "a", " ", "b"])
        self.assertEqual(rec["citations"], ["a", "b"])

    def test_lengths_clipped(self):
        rec = normalize_feedback("q" * 900, False, "other",
                                 note="n" * 900, answer="a" * 5000)
        self.assertEqual(len(rec["query"]), 500)
        self.assertEqual(len(rec["note"]), 500)
        self.assertEqual(len(rec["answer"]), 2000)

    def test_empty_query_rejected(self):
        with self.assertRaises(ValueError):
            normalize_feedback("   ", True)


class FeedbackFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "feedback.jsonl")
        rows = [
            {"ts": iso(timedelta(hours=1)), "query": "最新差评",
             "reason": "wrong_answer", "note": "答到别的题上了",
             "answer": "...", "citations": ["2019-408-01"], "helpful": False},
            {"ts": iso(timedelta(hours=2)), "query": "好评",
             "reason": "", "note": "", "answer": "...", "helpful": True},
            {"ts": iso(timedelta(hours=3)), "query": "次新差评",
             "reason": "bad_citation", "note": "", "answer": "...",
             "helpful": False},
            {"ts": iso(timedelta(days=40)), "query": "窗口外",
             "reason": "other", "helpful": False},
        ]
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write("这不是 JSON\n")
        self.stats = feedback_stats(self.path, days=30, limit=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts(self):
        self.assertEqual(self.stats["total"], 3)
        self.assertEqual(self.stats["helpful"], 1)
        self.assertEqual(self.stats["not_helpful"], 2)
        self.assertAlmostEqual(self.stats["helpful_rate"], 0.3333, places=4)

    def test_reasons(self):
        got = {r["reason"]: r["count"] for r in self.stats["by_reason"]}
        self.assertEqual(got, {"wrong_answer": 1, "bad_citation": 1})

    def test_recent_negative_only_and_limited(self):
        self.assertEqual(len(self.stats["recent"]), 1)
        self.assertEqual(self.stats["recent"][0]["query"], "最新差评")
        self.assertEqual(self.stats["recent"][0]["citations"], ["2019-408-01"])

    def test_log_roundtrip(self):
        rec = normalize_feedback("新问题", False, "should_answer",
                                 note="资料里有")
        row = log_feedback(self.path, rec)
        self.assertIn("ts", row)
        stats = feedback_stats(self.path, days=30, limit=10)
        self.assertEqual(stats["total"], 4)
        self.assertEqual({r["query"] for r in stats["recent"]},
                         {"最新差评", "次新差评", "新问题"})


if __name__ == "__main__":
    unittest.main()
