# -*- coding: utf-8 -*-
import json
import unittest

from answer.agent import QueryAgent, is_chitchat, normalize_history


class FakeProvider:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return json.dumps(self.payload, ensure_ascii=False)


class ChitchatTest(unittest.TestCase):
    def test_greeting(self):
        self.assertTrue(is_chitchat("你好"))
        self.assertTrue(is_chitchat("谢谢！"))
        self.assertTrue(is_chitchat("你能做什么？"))

    def test_question_is_not_chitchat(self):
        self.assertFalse(is_chitchat("什么是中断"))
        self.assertFalse(is_chitchat("谢谢，那 LRU 呢"))


class HistoryTest(unittest.TestCase):
    def test_drops_bad_roles_and_limits(self):
        hist = [{"role": "system", "content": "x"},
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": ""}]
        self.assertEqual([m["role"] for m in normalize_history(hist)],
                         ["user", "assistant"])

    def test_limits_to_tail(self):
        hist = [{"role": "user", "content": str(i)} for i in range(10)]
        self.assertEqual(normalize_history(hist, 3),
                         [{"role": "user", "content": "7"},
                          {"role": "user", "content": "8"},
                          {"role": "user", "content": "9"}])


class AgentTest(unittest.TestCase):
    def test_independent_question_skips_llm(self):
        p = FakeProvider({"intent": "kb", "query": "不应该被调用"})
        plan = QueryAgent(p).plan("什么是死锁", [{"role": "user", "content": "LRU"}])
        self.assertEqual(plan.source, "none")
        self.assertEqual(plan.rewritten, "什么是死锁")
        self.assertEqual(p.calls, 0)

    def test_follow_up_uses_llm_rewrite(self):
        p = FakeProvider({"intent": "kb", "query": "快速排序的平均时间复杂度",
                          "reason": "补全指代"})
        plan = QueryAgent(p).plan("那它的时间复杂度呢",
                                  [{"role": "user", "content": "快速排序"},
                                   {"role": "assistant", "content": "..."}])
        self.assertEqual(plan.intent, "kb")
        self.assertEqual(plan.source, "llm")
        self.assertEqual(plan.rewritten, "快速排序的平均时间复杂度")
        self.assertEqual(p.calls, 1)

    def test_clarify(self):
        p = FakeProvider({"intent": "clarify", "query": "", "reason": "缺少指代对象"})
        plan = QueryAgent(p).plan("那这个呢", [{"role": "user", "content": "上一题"}])
        self.assertEqual(plan.intent, "clarify")
        self.assertEqual(plan.source, "llm")

    def test_llm_failure_falls_back_to_rule(self):
        p = FakeProvider(error="timeout")
        plan = QueryAgent(p).plan("那时间复杂度呢",
                                  [{"role": "user", "content": "快速排序"}])
        self.assertEqual(plan.intent, "kb")
        self.assertEqual(plan.source, "rule")
        self.assertIn("快速排序", plan.rewritten)
        self.assertIn("那时间复杂度呢", plan.rewritten)

    def test_no_provider_falls_back_to_rule(self):
        plan = QueryAgent(None).plan("那它呢",
                                     [{"role": "user", "content": "LRU 页置换"}])
        self.assertEqual(plan.source, "rule")
        self.assertTrue(plan.rewritten.startswith("LRU 页置换"))

    def test_chitchat_skips_llm(self):
        p = FakeProvider({"intent": "kb", "query": "x"})
        plan = QueryAgent(p).plan("你好", [])
        self.assertEqual(plan.intent, "chitchat")
        self.assertEqual(p.calls, 0)


if __name__ == "__main__":
    unittest.main()
