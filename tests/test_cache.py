# -*- coding: utf-8 -*-
"""应答缓存的纯函数测试：不需要 PostgreSQL。"""
import os
import unittest
from unittest import mock

from answer.cache import (DEFAULT_TTL_S, cache_key, cached_payload,
                          is_fresh, normalize_query, storable_payload,
                          ttl_from_env, with_cache_meta)


class NormalizeTest(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(normalize_query("  快速\t排序\n时间复杂度  "),
                         "快速 排序 时间复杂度")

    def test_casefold(self):
        self.assertEqual(normalize_query("CSMA/CD"), normalize_query("csma/cd "))

    def test_empty(self):
        self.assertEqual(normalize_query(None), "")


class CacheKeyTest(unittest.TestCase):
    def test_stable_across_whitespace_and_case(self):
        a = cache_key("CSMA/CD 最小帧长", "hybrid-ce|deepseek")
        b = cache_key("  csma/cd   最小帧长 ", "hybrid-ce|deepseek")
        self.assertEqual(a, b)

    def test_namespace_isolated(self):
        a = cache_key("同一个问题", "hybrid-ce|deepseek")
        b = cache_key("同一个问题", "bm25|mock")
        self.assertNotEqual(a, b)

    def test_query_isolated(self):
        self.assertNotEqual(cache_key("问题一", "ns"), cache_key("问题二", "ns"))


class TtlTest(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ttl_from_env(), DEFAULT_TTL_S)

    def test_explicit(self):
        with mock.patch.dict(os.environ, {"KB_CACHE_TTL": "600"}):
            self.assertEqual(ttl_from_env(), 600)

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"KB_CACHE_TTL": "0"}):
            self.assertEqual(ttl_from_env(), 0)
        with mock.patch.dict(os.environ, {"KB_CACHE_TTL": "-5"}):
            self.assertEqual(ttl_from_env(), 0)

    def test_invalid_falls_back(self):
        with mock.patch.dict(os.environ, {"KB_CACHE_TTL": "半天"}):
            self.assertEqual(ttl_from_env(), DEFAULT_TTL_S)


class FreshTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertTrue(is_fresh(0, 60))
        self.assertTrue(is_fresh(60, 60))
        self.assertFalse(is_fresh(61, 60))

    def test_disabled(self):
        self.assertFalse(is_fresh(0, 0))
        self.assertFalse(is_fresh(0, -1))


class PayloadTest(unittest.TestCase):
    def _payload(self):
        return {
            "intent": "kb",
            "refused": False,
            "answer": "页目录号是 081H。",
            "citations": [{"chunk_id": "2019-408-31", "quote": "081H"}],
            "rewritten_query": "页目录号怎么算",
            "latency_ms": 6500,
            "trace": {"intent": "kb", "rewritten_query": "",
                      "steps": [{"name": "检索", "ms": 120, "detail": "..."}]},
        }

    def test_storable_drops_volatile_fields(self):
        stored = storable_payload(self._payload())
        self.assertNotIn("latency_ms", stored)
        self.assertNotIn("cached", stored)
        self.assertNotIn("cache_age_s", stored)
        self.assertEqual(stored["answer"], "页目录号是 081H。")
        self.assertEqual(stored["citations"][0]["chunk_id"], "2019-408-31")

    def test_miss_meta(self):
        out = with_cache_meta(self._payload())
        self.assertFalse(out["cached"])
        self.assertEqual(out["cache_age_s"], 0)

    def test_hit_meta_and_trace(self):
        payload = self._payload()
        out = cached_payload(payload, age_s=125.4, latency_ms=3)
        self.assertTrue(out["cached"])
        self.assertEqual(out["cache_age_s"], 125.4)
        self.assertEqual(out["latency_ms"], 3)
        self.assertEqual(out["answer"], payload["answer"])
        self.assertEqual(out["citations"], payload["citations"])
        self.assertEqual(len(out["trace"]["steps"]), 1)
        self.assertEqual(out["trace"]["steps"][0]["name"], "应答缓存")
        # 原始 payload 不能被就地修改
        self.assertNotIn("cached", payload)
        self.assertEqual(len(payload["trace"]["steps"]), 1)

    def test_hit_keeps_intent_and_rewrite(self):
        out = cached_payload(self._payload(), 10, 1)
        self.assertEqual(out["intent"], "kb")
        self.assertEqual(out["rewritten_query"], "页目录号怎么算")
        self.assertEqual(out["trace"]["rewritten_query"], "页目录号怎么算")


if __name__ == "__main__":
    unittest.main()
