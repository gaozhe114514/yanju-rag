# -*- coding: utf-8 -*-
"""缺口统计的 JSONL 实现测试：不需要 PostgreSQL。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from api.gap import gap_detail, gap_stats, reason_bucket


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


class ReasonBucketTest(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(reason_bucket("citation_failed: 引用过短"),
                         "citation_failed")
        self.assertEqual(reason_bucket("provider_error: timeout"),
                         "provider_error")

    def test_plain_reason(self):
        self.assertEqual(reason_bucket("no_hits"), "no_hits")
        self.assertEqual(reason_bucket("资料只给了选项"), "资料只给了选项")

    def test_empty(self):
        self.assertEqual(reason_bucket(""), "unknown")
        self.assertEqual(reason_bucket(None), "unknown")
        self.assertEqual(reason_bucket("  :  "), "unknown")


class GapDetailTest(unittest.TestCase):
    """模型说的缺口要能上台面；系统判定码不该冒充「资料缺什么」。"""

    def test_model_reason_passes(self):
        self.assertEqual(gap_detail("资料里没有 B+ 树删除的具体步骤"),
                         "资料里没有 B+ 树删除的具体步骤")

    def test_system_codes_are_silent(self):
        for reason in ("low_score: 0.12 < 0.20", "citation_failed: 引用过短",
                       "no_hits", "unknown", "server_not_ready"):
            self.assertEqual(gap_detail(reason), "", reason)

    def test_generic_default_is_silent(self):
        self.assertEqual(gap_detail("模型判断资料不足"), "")
        self.assertEqual(gap_detail(""), "")
        self.assertEqual(gap_detail(None), "")

    def test_whitespace_and_length(self):
        self.assertEqual(gap_detail("  资料\n缺\t第 8 章  "), "资料 缺 第 8 章")
        self.assertEqual(len(gap_detail("缺" * 400)), 160)


class GapStatsFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "gap.jsonl")
        rows = [
            {"ts": iso(timedelta(hours=2)), "query": "最新的问题",
             "reason": "low_score", "top_chunk_id": "2019-408-01",
             "top_score": 0.02},
            {"ts": iso(timedelta(hours=3)), "query": "次新的问题",
             "reason": "low_score", "top_chunk_id": "", "top_score": 0.0},
            {"ts": iso(timedelta(hours=5)), "query": "引用失败的问题",
             "reason": "citation_failed: 引用过短",
             "top_chunk_id": "2019-408-31", "top_score": 0.9},
            {"ts": iso(timedelta(days=40)), "query": "窗口外的问题",
             "reason": "no_hits", "top_chunk_id": "", "top_score": 0.0},
        ]
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write("这不是 JSON\n")
        self.stats = gap_stats(self.path, days=30, limit=2)

    def tearDown(self):
        self.tmp.cleanup()

    def test_total_excludes_old_and_bad_lines(self):
        self.assertEqual(self.stats["total"], 3)
        self.assertEqual(self.stats["window_days"], 30)
        self.assertEqual(self.stats["storage"], "jsonl")

    def test_by_reason_is_bucketed(self):
        by_reason = {r["reason"]: r["count"] for r in self.stats["by_reason"]}
        self.assertEqual(by_reason["low_score"], 2)
        self.assertEqual(by_reason["citation_failed"], 1)
        self.assertNotIn("no_hits", by_reason)

    def test_by_day(self):
        # 不假设三条记录落在同一个 UTC 日期（跨零点跑测试也稳定）
        self.assertEqual(sum(d["count"] for d in self.stats["by_day"]), 3)
        for row in self.stats["by_day"]:
            self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_recent_newest_first_and_limited(self):
        recent = self.stats["recent"]
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["query"], "最新的问题")
        self.assertEqual(recent[0]["top_chunk"], "2019-408-01")
        self.assertEqual(recent[1]["query"], "次新的问题")
        self.assertEqual(recent[1]["top_chunk"], None)  # 空 id 归一成 null

    def test_missing_file(self):
        stats = gap_stats(os.path.join(self.tmp.name, "nope.jsonl"))
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["recent"], [])


if __name__ == "__main__":
    unittest.main()
