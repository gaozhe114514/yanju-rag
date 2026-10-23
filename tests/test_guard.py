# -*- coding: utf-8 -*-
"""访问口令 + 限流的单元测试。"""
import os
import unittest
from datetime import date, timedelta
from unittest import mock

from api.guard import AccessGuard


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


def headers(token=None, xff=None):
    h = {}
    if token is not None:
        h["x-access-token"] = token
    if xff is not None:
        h["x-forwarded-for"] = xff
    return h


class TokenTest(unittest.TestCase):
    def test_no_token_configured_allows(self):
        g = AccessGuard()
        self.assertFalse(g.token_required)
        self.assertTrue(g.check({}, "1.1.1.1").ok)

    def test_missing_and_wrong_token(self):
        g = AccessGuard(token="s3cret")
        self.assertTrue(g.token_required)
        self.assertEqual(g.check({}, "1.1.1.1").status, 401)
        self.assertEqual(g.check(headers("nope"), "1.1.1.1").status, 401)
        self.assertTrue(g.check(headers("s3cret"), "1.1.1.1").ok)

    def test_non_ascii_token(self):
        g = AccessGuard(token="口令-408")
        self.assertTrue(g.check(headers("口令-408"), "1.1.1.1").ok)
        self.assertEqual(g.check(headers("口令"), "1.1.1.1").status, 401)

    def test_failed_token_does_not_consume_quota(self):
        g = AccessGuard(token="k", rate_max=1, clock=FakeClock())
        for _ in range(5):
            self.assertEqual(g.check(headers("bad"), "1.1.1.1").status, 401)
        self.assertTrue(g.check(headers("k"), "1.1.1.1").ok)
        self.assertEqual(g.check(headers("k"), "1.1.1.1").status, 429)


class RateTest(unittest.TestCase):
    def test_window_limits_and_recovers(self):
        clock = FakeClock()
        g = AccessGuard(rate_max=2, window=10, clock=clock)
        self.assertTrue(g.check({}, "ip").ok)
        self.assertTrue(g.check({}, "ip").ok)
        d = g.check({}, "ip")
        self.assertFalse(d.ok)
        self.assertEqual(d.status, 429)
        self.assertGreater(d.retry_after, 0)

        clock.tick(10.5)
        self.assertTrue(g.check({}, "ip").ok)

    def test_limits_are_per_ip(self):
        g = AccessGuard(rate_max=1, window=60, clock=FakeClock())
        self.assertTrue(g.check({}, "a").ok)
        self.assertEqual(g.check({}, "a").status, 429)
        self.assertTrue(g.check({}, "b").ok)

    def test_rate_max_zero_disables(self):
        g = AccessGuard(rate_max=0, window=60, clock=FakeClock())
        for _ in range(50):
            self.assertTrue(g.check({}, "a").ok)

    def test_stale_buckets_are_pruned(self):
        clock = FakeClock()
        g = AccessGuard(rate_max=10, window=10, clock=clock)
        for i in range(1500):
            g.check({}, "ip-%d" % i)
        self.assertEqual(len(g._hits), 1500)
        clock.tick(11)
        g.check({}, "fresh")
        self.assertLessEqual(len(g._hits), 2)


class DailyTest(unittest.TestCase):
    def test_daily_cap(self):
        g = AccessGuard(rate_max=0, daily_max=2)
        self.assertTrue(g.check({}, "a").ok)
        self.assertTrue(g.check({}, "b").ok)
        self.assertEqual(g.check({}, "c").status, 429)

    def test_daily_resets_next_day(self):
        g = AccessGuard(rate_max=0, daily_max=1)
        self.assertTrue(g.check({}, "a").ok)
        self.assertEqual(g.check({}, "b").status, 429)
        g._day = date.today() - timedelta(days=1)
        self.assertEqual(g._daily_used, 1)
        self.assertTrue(g.check({}, "c").ok)
        self.assertEqual(g._daily_used, 1)


class ClientIpTest(unittest.TestCase):
    def test_ignores_xff_by_default(self):
        g = AccessGuard()
        self.assertEqual(g.client_ip(headers(xff="9.9.9.9"), "10.0.0.1"), "10.0.0.1")

    def test_trusts_first_xff_when_enabled(self):
        g = AccessGuard(trust_proxy=True)
        self.assertEqual(g.client_ip({"x-forwarded-for": "9.9.9.9, 10.0.0.2"},
                                     "10.0.0.1"), "9.9.9.9")

    def test_accepts_capitalized_header(self):
        g = AccessGuard(trust_proxy=True)
        self.assertEqual(g.client_ip({"X-Forwarded-For": "7.7.7.7"}, "10.0.0.1"),
                         "7.7.7.7")

    def test_xff_separates_rate_budgets(self):
        g = AccessGuard(rate_max=1, window=60, trust_proxy=True, clock=FakeClock())
        self.assertTrue(g.check(headers(xff="9.9.9.9"), "10.0.0.1").ok)
        self.assertEqual(g.check(headers(xff="9.9.9.9"), "10.0.0.1").status, 429)
        self.assertTrue(g.check(headers(xff="8.8.8.8"), "10.0.0.1").ok)


class FromEnvTest(unittest.TestCase):
    def test_from_env(self):
        env = {
            "KB_ACCESS_TOKEN": "t",
            "KB_RATE_MAX": "5",
            "KB_RATE_WINDOW": "60",
            "KB_DAILY_MAX": "7",
            "KB_TRUST_PROXY": "1",
        }
        with mock.patch.dict(os.environ, env):
            g = AccessGuard.from_env()
        self.assertEqual(g.token, "t")
        self.assertEqual(g.rate_max, 5)
        self.assertEqual(g.window, 60)
        self.assertEqual(g.daily_max, 7)
        self.assertTrue(g.trust_proxy)


if __name__ == "__main__":
    unittest.main()
