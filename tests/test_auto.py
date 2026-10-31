# -*- coding: utf-8 -*-
"""单一问答链路：资料查不到依据时，改给「标明未查资料」的通用回答。"""
import unittest

from answer.auto import fallback_detail, should_fallback
from answer.daily import (DAILY_SYSTEM, GENERAL_PREFIX, GENERAL_SYSTEM,
                          OFFLINE_REPLY, build_general_messages,
                          general_reply, general_stream)
from answer.pipeline import AnswerResult
from answer.providers import ProviderError


class ShouldFallbackTest(unittest.TestCase):
    def test_gate_refusals_fall_back(self):
        for reason in ("low_score", "no_hits", "ambiguous", "low_margin: 0.01"):
            self.assertTrue(should_fallback(AnswerResult(True, reason=reason)),
                            reason)

    def test_model_judged_gap_falls_back(self):
        self.assertTrue(should_fallback(
            AnswerResult(True, reason="资料只讲了定义，没有删除步骤")))

    def test_citation_failure_stays_refused(self):
        # 引用纪律是硬规则：校验不过必须拒答，不能拿通用回答顶替
        self.assertFalse(should_fallback(
            AnswerResult(True, reason="citation_failed: quote 不在原文里")))

    def test_provider_errors_stay_refused(self):
        for reason in ("provider_error: boom", "server_not_ready",
                       "empty_collection"):
            self.assertFalse(should_fallback(AnswerResult(True, reason=reason)),
                             reason)

    def test_answered_is_not_a_fallback(self):
        self.assertFalse(should_fallback(AnswerResult(False, answer="好的")))

    def test_detail_explains_why(self):
        self.assertIn("召回分数", fallback_detail("low_score: 0.02"))
        self.assertIn("没有召回到资料", fallback_detail("no_hits"))
        self.assertIn("通用知识", fallback_detail("模型判断资料不足"))


class FakeProvider:
    def __init__(self, text="408 考四门。", stream=("408 ", "考四门。")):
        self.text = text
        self.stream = stream
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.text

    def chat_stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        for piece in self.stream:
            yield piece


class GeneralMessagesTest(unittest.TestCase):
    def test_system_prompt_is_not_the_daily_one(self):
        msgs = build_general_messages("408 都是哪几门")
        self.assertEqual(msgs[0]["content"], GENERAL_SYSTEM)
        self.assertNotEqual(GENERAL_SYSTEM, DAILY_SYSTEM)
        self.assertIn("资料里没有直接依据", GENERAL_SYSTEM)
        self.assertEqual(msgs[-1], {"role": "user", "content": "408 都是哪几门"})

    def test_daily_prompt_no_longer_talks_about_modes(self):
        self.assertNotIn("资料模式", DAILY_SYSTEM)
        self.assertNotIn("日常模式", DAILY_SYSTEM)
        self.assertIn("不要编造出处", DAILY_SYSTEM)


class GeneralReplyTest(unittest.TestCase):
    def test_prefix_is_always_added(self):
        text = general_reply(FakeProvider(), "408 都是哪几门")
        self.assertTrue(text.startswith(GENERAL_PREFIX))
        self.assertIn("408 考四门。", text)

    def test_model_is_told_not_to_repeat_the_prefix(self):
        p = FakeProvider()
        general_reply(p, "q")
        msgs, kwargs = p.calls[0]
        self.assertEqual(msgs[0]["content"], GENERAL_SYSTEM)
        self.assertIn("不要重复这句", msgs[0]["content"])
        self.assertFalse(kwargs["json_mode"])

    def test_offline_mode_is_honest(self):
        text = general_reply(None, "q")
        self.assertTrue(text.startswith(GENERAL_PREFIX))
        self.assertIn(OFFLINE_REPLY, text)

    def test_stream_starts_with_prefix(self):
        pieces = list(general_stream(FakeProvider(), "q"))
        self.assertEqual(pieces[0], GENERAL_PREFIX)
        self.assertEqual("".join(pieces[1:]), "408 考四门。")

    def test_stream_offline_after_prefix(self):
        pieces = list(general_stream(None, "q"))
        self.assertEqual(pieces, [GENERAL_PREFIX, OFFLINE_REPLY])

    def test_provider_error_propagates(self):
        class Boom(FakeProvider):
            def complete(self, messages, **kwargs):
                raise ProviderError("boom")

        with self.assertRaises(ProviderError):
            general_reply(Boom(), "q")


if __name__ == "__main__":
    unittest.main()
