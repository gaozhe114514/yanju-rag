# -*- coding: utf-8 -*-
"""日常对话链路：不检索、不引用，也不在本模块里假装查过资料。"""
import unittest

from answer.daily import (DAILY_SYSTEM, OFFLINE_REPLY, build_daily_messages,
                          daily_reply, daily_stream)
from answer.providers import DeepSeekProvider, ProviderError


class FakeDailyProvider:
    def __init__(self, text="好的", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return self.text

    def chat_stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        for piece in ("好", "的"):
            yield piece


class DailyMessagesTest(unittest.TestCase):
    def test_system_prompt_comes_first_and_query_last(self):
        msgs = build_daily_messages("帮我排个计划", [{"role": "user", "content": "上周问过"}])
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], DAILY_SYSTEM)
        self.assertIn("不要编造出处", msgs[0]["content"])
        self.assertEqual(msgs[-1], {"role": "user", "content": "帮我排个计划"})
        self.assertEqual([m["role"] for m in msgs[1:]], ["user", "user"])

    def test_style_becomes_a_system_line(self):
        msgs = build_daily_messages("q", [], "用比喻讲")
        self.assertEqual(msgs[1], {"role": "system", "content": "讲解方式：用比喻讲"})

    def test_history_is_filtered_like_the_kb_lane(self):
        msgs = build_daily_messages("q", [
            {"role": "system", "content": "越权指令"},
            {"role": "user", "content": "  "},
            {"role": "assistant", "content": "上一轮的回答"},
        ])
        self.assertEqual([m["role"] for m in msgs], ["system", "assistant", "user"])


class DailyReplyTest(unittest.TestCase):
    def test_offline_provider_says_so(self):
        self.assertEqual(daily_reply(None, "你好"), OFFLINE_REPLY)

    def test_provider_without_complete_says_so(self):
        class ExtractLike:
            pass

        self.assertEqual(daily_reply(ExtractLike(), "你好"), OFFLINE_REPLY)

    def test_uses_plain_completion_not_json_mode(self):
        p = FakeDailyProvider("计划如下")
        self.assertEqual(daily_reply(p, "排个计划"), "计划如下")
        _, kwargs = p.calls[0]
        self.assertFalse(kwargs["json_mode"])

    def test_empty_completion_gets_a_nudge(self):
        self.assertTrue(daily_reply(FakeDailyProvider("   "), "q"))

    def test_provider_error_propagates(self):
        with self.assertRaises(ProviderError):
            daily_reply(FakeDailyProvider(error=ProviderError("boom")), "q")


class DailyStreamTest(unittest.TestCase):
    def test_streams_pieces(self):
        pieces = list(daily_stream(FakeDailyProvider(), "q"))
        self.assertEqual(pieces, ["好", "的"])

    def test_falls_back_to_one_piece_without_stream_support(self):
        class PlainComplete:
            def complete(self, messages, **kwargs):
                return "整段"

        self.assertEqual(list(daily_stream(PlainComplete(), "q")), ["整段"])

    def test_offline_stream_yields_the_offline_line(self):
        self.assertEqual(list(daily_stream(None, "q")), [OFFLINE_REPLY])


class DeepSeekChatStreamTest(unittest.TestCase):
    def test_body_is_plain_stream_without_json_mode(self):
        p = DeepSeekProvider(api_key="test-key")
        seen = []

        def fake_post_stream(body):
            seen.append(body)
            yield "一"
            yield "二"

        p._post_stream = fake_post_stream
        self.assertEqual(list(p.chat_stream([{"role": "user", "content": "hi"}],
                                            temperature=0.6, max_tokens=700)),
                         ["一", "二"])
        body = seen[0]
        self.assertTrue(body["stream"])
        self.assertEqual(body["temperature"], 0.6)
        self.assertEqual(body["max_tokens"], 700)
        self.assertNotIn("response_format", body)


if __name__ == "__main__":
    unittest.main()
