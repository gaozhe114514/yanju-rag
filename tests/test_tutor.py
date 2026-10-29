# -*- coding: utf-8 -*-
"""精通之路的导师会话：拼装、清洗、重试与离线兜底。

这里不测模型说得好不好（那要靠真实模型跑），只测我们自己的边界：
返回的一轮结果必须能被前端安全地画出来，改大纲时不能把已有进度抹掉。
"""
import json
import unittest

from answer.providers import ProviderError
from answer.tutor import (TUTOR_SYSTEM, build_turn_messages, clean_ask,
                          clean_outline, clean_profile, offline_turn,
                          run_turn)


def outline(modules):
    return {"name": "线性代数", "modules": modules}


def mod(name, points, objective="学完能独立做题"):
    return {"name": name, "objective": objective,
            "points": [{"name": p, "type": "concept"} for p in points]}


class FakeProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class MessagesTest(unittest.TestCase):
    def payload(self, **extra):
        base = {
            "goal": "学会线代",
            "subject": "考研数学",
            "learner": {"prior": "纯新手"},
            "outline": {"modules": []},
            "messages": [{"role": "user", "content": "我基础差"},
                         {"role": "assistant", "content": "先问几个问题"}],
            "message": "帮我排一下",
        }
        base.update(extra)
        return base

    def test_system_prompt_first_and_context_last(self):
        msgs = build_turn_messages(self.payload(),
                                   [{"label": "考点卡", "text": "链表先进先出", "titles": ["链表"]}])
        self.assertEqual(msgs[0], {"role": "system", "content": TUTOR_SYSTEM})
        self.assertIn("不要编出处", TUTOR_SYSTEM)
        ctx = msgs[1]["content"]
        self.assertIn("学会线代", ctx)
        self.assertIn("● 考点卡", ctx)
        self.assertIn("现在已经会什么：纯新手", ctx)
        self.assertIn("我基础差", ctx)
        self.assertTrue(ctx.rstrip().endswith("帮我排一下"))

    def test_empty_state_says_so_instead_of_blank(self):
        msgs = build_turn_messages(self.payload(learner={}, messages=[], message=""), [])
        ctx = msgs[1]["content"]
        self.assertIn("（还没有回答过", ctx)
        self.assertIn("没有勾选资料", ctx)
        self.assertIn("刚建好这个目标", ctx)

    def test_existing_outline_is_passed_in(self):
        msgs = build_turn_messages(self.payload(outline=outline([
            mod("基础", ["链表", "栈"])])), [])
        self.assertIn("【当前大纲】", msgs[1]["content"])
        self.assertIn("链表", msgs[1]["content"])


class CleanAskTest(unittest.TestCase):
    def test_normalises_questions_and_drops_catch_all(self):
        ask = clean_ask({"intro": "先问几句", "questions": [
            {"prompt": "你现在什么水平？", "header": "基础",
             "options": [{"label": "零基础", "description": "没接触过"},
                         {"label": "其他", "description": "..."},
                         "学过一遍"]},
        ]})
        self.assertEqual(ask["intro"], "先问几句")
        q = ask["questions"][0]
        self.assertEqual(q["id"], "q1")
        self.assertEqual([o["label"] for o in q["options"]], ["零基础", "学过一遍"])
        self.assertEqual(q["options"][1]["description"], "")

    def test_caps_questions_and_options(self):
        ask = clean_ask({"questions": [
            {"prompt": "问 %d" % i,
             "options": [{"label": "选项 %d" % j} for j in range(10)]}
            for i in range(6)]})
        self.assertEqual(len(ask["questions"]), 4)
        self.assertEqual(len(ask["questions"][0]["options"]), 6)

    def test_rejects_empty_or_invalid(self):
        self.assertIsNone(clean_ask(None))
        self.assertIsNone(clean_ask({"questions": []}))
        self.assertIsNone(clean_ask({"questions": [{"prompt": "  "}]}))


class CleanProfileTest(unittest.TestCase):
    def test_only_known_keys_and_clipped(self):
        prof = clean_profile({"prior": "  学过一遍 ", "hobby": "篮球",
                              "time": "x" * 500})
        self.assertEqual(prof["prior"], "学过一遍")
        self.assertNotIn("hobby", prof)
        self.assertEqual(len(prof["time"]), 200)

    def test_empty_input(self):
        self.assertEqual(clean_profile(None), {})
        self.assertEqual(clean_profile({"prior": "  "}), {})


class CleanOutlineTest(unittest.TestCase):
    def test_assigns_ids_and_rejects_too_small(self):
        ok = clean_outline(outline([
            mod("基础", ["数组", "链表"]), mod("树", ["遍历", "BST"])]))
        self.assertEqual(len(ok["modules"]), 2)
        self.assertEqual(ok["modules"][0]["id"], "m1")
        self.assertEqual(ok["modules"][0]["points"][0]["id"], "m1-p1")
        self.assertIsNone(clean_outline(outline([mod("基础", ["数组", "链表"])])))
        self.assertIsNone(clean_outline(outline([
            mod("基础", ["数组"]), mod("树", ["遍历"])])))

    def test_revision_keeps_progress_and_ids(self):
        before = clean_outline(outline([
            mod("基础", ["数组", "链表"]), mod("树", ["遍历", "BST"])]))
        before["modules"][0]["points"][0]["done"] = True
        revised = clean_outline(outline([
            mod("基础", ["数组", "链表", "栈"]), mod("树", ["遍历", "BST", "AVL"])]),
            previous=before)
        self.assertEqual(revised["modules"][0]["points"][0]["id"], "m1-p1")
        self.assertTrue(revised["modules"][0]["points"][0]["done"])
        new_point = revised["modules"][0]["points"][2]
        self.assertFalse(new_point["done"])
        self.assertTrue(new_point["id"].startswith("m1-p"))

    def test_caps_and_drops_duplicates(self):
        many = [mod("模块 %d" % i, ["点 %d-%d" % (i, j) for j in range(10)])
                for i in range(14)]
        cleaned = clean_outline(outline(many))
        self.assertLessEqual(len(cleaned["modules"]), 10)
        self.assertLessEqual(
            sum(len(m["points"]) for m in cleaned["modules"]), 48)
        dup = clean_outline(outline([
            {"name": "基础", "objective": "", "knowledge_points":
                [{"name": "数组"}, {"name": "数组"}, {"name": "链表"}]},
            mod("树", ["遍历", "BST"])]))
        self.assertEqual(len(dup["modules"][0]["points"]), 2)

    def test_unknown_type_falls_back_to_concept(self):
        cleaned = clean_outline(outline([
            {"name": "A", "knowledge_points": [{"name": "x", "type": "weird"},
                                               {"name": "y", "type": "memory"}]},
            {"name": "B", "knowledge_points": [{"name": "z"}, {"name": "w"}]}]))
        types = [p["type"] for m in cleaned["modules"] for p in m["points"]]
        self.assertEqual(types, ["concept", "memory", "concept", "concept"])


class RunTurnTest(unittest.TestCase):
    payload = {
        "goal": "学会线代", "subject": "数学", "learner": {},
        "outline": {"modules": []}, "messages": [], "message": "开始吧",
    }

    def test_ask_round(self):
        provider = FakeProvider([json.dumps({
            "say": "先了解你几句",
            "ask": {"intro": "", "questions": [
                {"id": "prior", "header": "基础", "prompt": "你现在什么水平？",
                 "options": [{"label": "零基础"}, {"label": "学过一遍"}]}]},
            "profile": None, "outline": None, "stage": "intake",
        }, ensure_ascii=False)])
        result = run_turn(provider, self.payload, [])
        self.assertEqual(result["stage"], "intake")
        self.assertEqual(result["ask"]["questions"][0]["id"], "prior")
        self.assertIsNone(result["outline"])
        _, kwargs = provider.calls[0]
        self.assertTrue(kwargs["json_mode"])

    def test_outline_round_sets_draft(self):
        provider = FakeProvider([json.dumps({
            "say": "这版大纲你先看看",
            "outline": outline([mod("基础", ["数组", "链表"]),
                                mod("树", ["遍历", "BST"])]),
            "stage": "intake",
        }, ensure_ascii=False)])
        result = run_turn(provider, self.payload, [])
        self.assertEqual(result["stage"], "draft")
        self.assertEqual(len(result["outline"]["modules"]), 2)

    def test_approved_stage_passes_through(self):
        provider = FakeProvider([json.dumps({
            "say": "大纲定稿了", "stage": "approved",
            "outline": outline([mod("基础", ["数组", "链表"]),
                                mod("树", ["遍历", "BST"])]),
        }, ensure_ascii=False)])
        self.assertEqual(run_turn(provider, self.payload, [])["stage"], "approved")

    def test_retries_once_on_bad_json(self):
        provider = FakeProvider([
            "这不是 JSON",
            json.dumps({"say": "重来一次", "stage": "intake"}, ensure_ascii=False),
        ])
        result = run_turn(provider, self.payload, [])
        self.assertEqual(result["reply"], "重来一次")
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("JSON", provider.calls[1][0][-1]["content"])

    def test_two_bad_outputs_raise(self):
        provider = FakeProvider(["坏的", "还是坏的"])
        with self.assertRaises(ProviderError):
            run_turn(provider, self.payload, [])

    def test_think_blocks_are_stripped_from_reply(self):
        provider = FakeProvider([json.dumps({
            "say": "<think>该不该问</think>先问一句", "stage": "intake",
        }, ensure_ascii=False)])
        self.assertEqual(run_turn(provider, self.payload, [])["reply"], "先问一句")


class OfflineTurnTest(unittest.TestCase):
    def test_asks_first_when_profile_is_empty(self):
        result = offline_turn({"learner": {}, "goal": "学线代"}, [])
        self.assertTrue(result["offline"])
        self.assertEqual(len(result["ask"]["questions"]), 4)
        self.assertIsNone(result["outline"])
        self.assertIn("离线演示模式", result["reply"])

    def test_builds_outline_from_titles_after_two_answers(self):
        digest = [
            {"label": "数据结构考点", "kind": "knowledge", "text": "…",
             "titles": ["线性表", "栈与队列"]},
            {"label": "计组考点", "kind": "knowledge", "text": "…",
             "titles": ["存储系统", "指令系统"]},
        ]
        payload = {"learner": {"prior": "学过一遍", "target": "会做题"},
                   "goal": "补 408"}
        result = offline_turn(payload, digest)
        self.assertEqual(result["stage"], "draft")
        self.assertEqual(len(result["outline"]["modules"]), 2)
        self.assertEqual(result["outline"]["modules"][0]["points"][0]["name"], "线性表")

    def test_says_so_when_materials_cannot_make_an_outline(self):
        result = offline_turn({"learner": {"prior": "a", "target": "b"}},
                              [{"label": "空资料", "text": "", "titles": []}])
        self.assertIsNone(result["outline"])
        self.assertIn("拼成知识点", result["reply"])


if __name__ == "__main__":
    unittest.main()
