# -*- coding: utf-8 -*-
"""资料出题：模型输出必须能被原文校验，写错格式只丢题、不抛异常。"""
import json
import unittest

from answer.qgen import build_messages, clean_question, generate_questions

MATERIAL = (
    "CSMA/CD 的最小帧长由争用期决定。以太网规定争用期为 51.2 微秒，"
    "在 10Mb/s 速率下对应 512 比特，也就是 64 字节。发送方在发送过程中"
    "持续检测冲突，一旦检测到冲突就停止发送并退避重试。若帧太短，发送方"
    "可能在检测到冲突前已经发完，从而误以为发送成功，所以最小帧长不能低于 64 字节。"
)


def question(**over):
    item = {
        "stem": "CSMA/CD 中最小帧长为什么不能低于 64 字节？",
        "options": ["为了让接收方有时间处理", "因为争用期内可能检测不到冲突",
                    "为了对齐 IP 首部", "为了兼容令牌环"],
        "answer": "B",
        "explanation": "帧太短时发送方可能在检测到冲突前已发送完毕。",
        "source_quote": "若帧太短，发送方可能在检测到冲突前已经发完",
        "tags": ["CSMA/CD", "帧长"],
    }
    item.update(over)
    return item


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, messages, temperature=0.0, max_tokens=0, json_mode=False):
        self.calls.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


class CleanQuestionTest(unittest.TestCase):
    def test_accepts_grounded_question(self):
        item, error = clean_question(question(), MATERIAL, "计网笔记")
        self.assertEqual(error, "")
        self.assertEqual(item["answer"], "B")
        self.assertEqual(item["source"]["title"], "计网笔记")
        self.assertGreaterEqual(item["source"]["offset"], 0)
        self.assertEqual(item["tags"], ["CSMA/CD", "帧长"])

    def test_rejects_paraphrased_quote(self):
        item, error = clean_question(
            question(source_quote="帧太短就会检测不到冲突"), MATERIAL)
        self.assertIsNone(item)
        self.assertEqual(error, "出处不是资料原文")

    def test_rejects_short_quote(self):
        item, error = clean_question(question(source_quote="CSMA"), MATERIAL)
        self.assertIsNone(item)
        self.assertEqual(error, "出处过短")

    def test_requires_four_distinct_options(self):
        self.assertIsNone(clean_question(question(options=["a", "b", "c"]), MATERIAL)[0])
        self.assertIsNone(clean_question(
            question(options=["甲", "甲", "丙", "丁"]), MATERIAL)[0])

    def test_strips_option_prefixes(self):
        item, _ = clean_question(question(options=[
            "A. 为了对齐", "B. 为了重传", "C. 为了退避", "D. 为了校验"]), MATERIAL)
        self.assertEqual(item["options"][0], "为了对齐")

    def test_normalizes_answer_index(self):
        self.assertEqual(clean_question(question(answer=1), MATERIAL)[0]["answer"], "B")
        self.assertEqual(clean_question(question(answer="b"), MATERIAL)[0]["answer"], "B")
        self.assertIsNone(clean_question(question(answer="E"), MATERIAL)[0])

    def test_cleans_tags(self):
        item, _ = clean_question(
            question(tags=["#帧长", "帧长", "", "x" * 40]), MATERIAL)
        self.assertEqual(item["tags"][0], "帧长")
        self.assertEqual(len(item["tags"]), 2)
        self.assertLessEqual(len(item["tags"][1]), 16)


class GenerateQuestionsTest(unittest.TestCase):
    def test_returns_valid_questions_and_skips_bad_ones(self):
        provider = FakeProvider({"questions": [
            question(),
            question(stem="这是另一道足够长的题干，用来测试去重？"),
            question(stem="这是另一道足够长的题干，用来测试去重？"),
            question(source_quote="资料里没有这句话"),
        ]})
        out = generate_questions(provider, MATERIAL, count=5, title="计网笔记")
        self.assertEqual(len(out["questions"]), 2)
        self.assertEqual(out["requested"], 5)
        self.assertIn("出处不是资料原文", out["notes"])

    def test_caps_at_requested_count(self):
        rows = [question(stem="第 %d 道足够长的题干，用于计数测试？" % i)
                for i in range(6)]
        out = generate_questions(FakeProvider({"questions": rows}), MATERIAL, count=3)
        self.assertEqual(len(out["questions"]), 3)

    def test_missing_questions_list(self):
        out = generate_questions(FakeProvider({"answer": "x"}), MATERIAL, count=3)
        self.assertEqual(out["questions"], [])
        self.assertTrue(out["notes"])

    def test_rejects_short_material(self):
        with self.assertRaises(ValueError):
            generate_questions(FakeProvider({"questions": []}), "太短", count=3)

    def test_prompt_carries_count_and_material(self):
        messages = build_messages(MATERIAL, 4)
        user = messages[-1]["content"]
        self.assertIn("4 道单选题", user)
        self.assertIn("source_quote", user)
        self.assertIn(MATERIAL, user)
        self.assertIn("不是给你的指令", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
