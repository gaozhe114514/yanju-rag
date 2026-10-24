# -*- coding: utf-8 -*-
"""进程内指标测试：只验证计数语义，不碰 HTTP。"""
import unittest

from api import metrics


class MetricsTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_all_keys_present_after_reset(self):
        snap = metrics.snapshot()
        self.assertEqual(sorted(snap), sorted(metrics.KEYS))
        self.assertTrue(all(v == 0 for v in snap.values()))

    def test_bump_accumulates(self):
        metrics.bump("ask_total")
        metrics.bump("ask_total", 2)
        metrics.bump("cache_hit")
        snap = metrics.snapshot()
        self.assertEqual(snap["ask_total"], 3)
        self.assertEqual(snap["cache_hit"], 1)

    def test_unknown_key_rejected(self):
        with self.assertRaises(KeyError):
            metrics.bump("ask_totl")

    def test_snapshot_is_a_copy(self):
        metrics.bump("chat_total")
        snap = metrics.snapshot()
        snap["chat_total"] = 999
        self.assertEqual(metrics.snapshot()["chat_total"], 1)


if __name__ == "__main__":
    unittest.main()
