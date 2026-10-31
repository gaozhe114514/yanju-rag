# -*- coding: utf-8 -*-
"""学习 / 复习模式的引擎规则：模式归一、掌握门槛、判分、出题清洗。

这些是纯函数和离线路径，不依赖真实模型。模型只负责"讲和出题"，
"算不算掌握"必须由这里决定，所以每一条边界都钉住。
"""
import json
import unittest

from answer.tutor import (clean_assess, clean_quiz, current_point,
                          engine_mastered, gate_status, grade_choice,
                          normalize_mode, run_session_turn, run_turn, wants_study)


def outline(points, module="第一条"):
    return {"name": "线性代数", "modules": [{
        "id": "m1", "name": module, "objective": "学完能独立做题",
        "points": points}]}


def point(pid="p1", kind="procedure", attempts=None, done=False, name="二分查找"):
    return {"id": pid, "name": name, "type": kind, "done": done,
            "attempts": list(attempts or []), "masteredAt": 0, "manual": False}


def answer(choice="B", qid="q-1", text=""):
    return {"questionId": qid, "choice": choice, "text": text}


def attempt(ok, diff=2, kind="quiz", at=0):
    return {"ok": ok, "diff": diff, "kind": kind, "at": at}


def pending(key="B", diff=2, qid="q-1", kp="p1", kind="quiz"):
    return {"questionId": qid, "kpId": kp, "kind": kind, "prompt": "二分查找在哪种情况下退化？",
            "options": [{"label": "A", "body": "数组已排序时"},
                        {"label": "B", "body": "数组接近有序时"},
                        {"label": "C", "body": "数组完全逆序时"}],
            "key": key, "explanation": "接近有序会让划分失衡。", "difficulty": diff}


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


class GateStatusTest(unittest.TestCase):
    """界面要能解释「为什么还没掌握」，不能只给一个「没过」。"""

    def test_missing_evidence_is_named(self):
        gate = gate_status(point(attempts=[attempt(True, 3)]))
        self.assertFalse(gate["mastered"])
        self.assertTrue(any("至少要有 2 次" in x for x in gate["need"]))

    def test_difficulty_floor_is_named(self):
        gate = gate_status(point(attempts=[attempt(True, 1), attempt(True, 1)]))
        self.assertTrue(any("难度 ≥2" in x for x in gate["need"]))

    def test_last_wrong_is_named(self):
        gate = gate_status(point(attempts=[attempt(True, 3), attempt(False, 2)]))
        self.assertTrue(any("最近一次答错" in x for x in gate["need"]))

    def test_low_score_is_named(self):
        gate = gate_status(point(attempts=[attempt(True, 2), attempt(False, 2),
                                           attempt(False, 2), attempt(False, 2)]))
        self.assertTrue(any("正确率" in x for x in gate["need"]))

    def test_concept_needs_own_words(self):
        gate = gate_status(point(kind="concept", attempts=[attempt(True, 3)]))
        self.assertEqual(gate["kind"], "assess")
        self.assertTrue(any("用自己的话" in x for x in gate["need"]))

    def test_mastered_has_nothing_left(self):
        gate = gate_status(point(attempts=[attempt(True, 3), attempt(True, 2)]))
        self.assertTrue(gate["mastered"])
        self.assertEqual(gate["need"], [])

    def test_grade_carries_the_gate(self):
        body = {"goal": "x", "outline": outline([point()]),
                "pending": pending(diff=1), "answer": answer("A"), "now": 1000,
                "message": "A", "mode": "study", "messages": []}
        result = run_session_turn(
            FakeProvider([json.dumps({"say": "讲一段。", "quiz": None, "assess": None})]),
            body, [], "study")
        self.assertFalse(result["grade"]["mastered"])
        self.assertTrue(result["grade"]["gate"]["need"])


class NormalizeModeTest(unittest.TestCase):
    def test_known_modes_win(self):
        empty = {"modules": []}
        self.assertEqual(normalize_mode("review", empty), "review")
        self.assertEqual(normalize_mode(" STUDY ", empty), "study")

    def test_guess_from_outline_when_missing(self):
        self.assertEqual(normalize_mode(None, {"modules": []}), "outline")
        self.assertEqual(normalize_mode("", outline([point()])), "study")

    def test_unknown_falls_back(self):
        self.assertEqual(normalize_mode("quiz", {"modules": []}), "outline")


class EngineMasteredTest(unittest.TestCase):
    def test_needs_minimum_evidence(self):
        self.assertFalse(engine_mastered(point(attempts=[attempt(True, 3)])))

    def test_two_recent_correct_with_difficulty(self):
        p = point(attempts=[attempt(True, 1), attempt(True, 2)])
        self.assertTrue(engine_mastered(p))

    def test_last_answer_must_be_right(self):
        p = point(attempts=[attempt(True, 2), attempt(True, 2), attempt(False, 2)])
        self.assertFalse(engine_mastered(p))

    def test_low_score_fails(self):
        p = point(attempts=[attempt(False, 2), attempt(False, 2),
                            attempt(True, 2), attempt(True, 2)])
        # 4 条里对 2 条，最近窗口 50% < 75%
        self.assertFalse(engine_mastered(p))

    def test_difficulty_floor(self):
        p = point(attempts=[attempt(True, 1), attempt(True, 1)])
        self.assertFalse(engine_mastered(p))

    def test_concept_needs_own_words(self):
        p = point(kind="concept", attempts=[attempt(True, 5), attempt(True, 5)])
        self.assertFalse(engine_mastered(p))
        p["attempts"].append(attempt(True, 3, kind="assess"))
        self.assertTrue(engine_mastered(p))

    def test_assess_does_not_open_procedure_gate(self):
        p = point(kind="procedure", attempts=[attempt(True, 3, kind="assess")])
        self.assertFalse(engine_mastered(p))

    def test_current_point_skips_mastered(self):
        first = point("p1", attempts=[attempt(True, 2), attempt(True, 2)])
        second = point("p2")
        target = current_point(outline([first, second]))
        self.assertEqual(target["point"]["id"], "p2")


class GradeChoiceTest(unittest.TestCase):
    def test_label_is_case_and_suffix_insensitive(self):
        self.assertTrue(grade_choice(pending(key="B"), {"choice": "b"}))
        self.assertTrue(grade_choice(pending(key="B"), {"choice": "B. 数组接近有序时"}))
        self.assertFalse(grade_choice(pending(key="B"), {"choice": "C"}))

    def test_empty_choice_never_right(self):
        self.assertFalse(grade_choice(pending(key="B"), {"choice": ""}))
        self.assertFalse(grade_choice(pending(key=""), {"choice": ""}))


class CleanQuizTest(unittest.TestCase):
    def test_requires_prompt_and_two_bodied_options(self):
        self.assertIsNone(clean_quiz({"prompt": "题干"}, outline([point()])))
        self.assertIsNone(clean_quiz({"prompt": "题干", "options": [
            {"label": "A", "body": ""}, {"label": "B", "body": "正文"}], "key": "B"},
            outline([point()])))

    def test_key_given_as_body_text(self):
        q = clean_quiz({"prompt": "题干", "options": [
            {"label": "A", "body": "第一条正文"}, {"label": "B", "body": "第二条正文"}],
            "key": "第二条正文"}, outline([point()]))
        self.assertEqual(q["key"], "B")

    def test_bad_key_rejected(self):
        self.assertIsNone(clean_quiz({"prompt": "题干", "options": [
            {"label": "A", "body": "甲"}, {"label": "B", "body": "乙"}], "key": "D"},
            outline([point()])))

    def test_difficulty_clamped_and_kp_defaults_to_current(self):
        q = clean_quiz({"prompt": "题干", "options": [
            {"label": "A", "body": "甲"}, {"label": "B", "body": "乙"}],
            "key": "A", "difficulty": 9}, outline([point()]))
        self.assertEqual(q["difficulty"], 5)
        self.assertEqual(q["kpId"], "p1")
        self.assertEqual(q["kind"], "quiz")
        self.assertTrue(q["questionId"].startswith("q-"))

    def test_same_question_gets_same_id(self):
        raw = {"prompt": "题干", "options": [{"label": "A", "body": "甲"},
                                             {"label": "B", "body": "乙"}],
               "key": "A"}
        first = clean_quiz(json.loads(json.dumps(raw)), outline([point()]))
        second = clean_quiz(json.loads(json.dumps(raw)), outline([point()]))
        self.assertEqual(first["questionId"], second["questionId"])

    def test_option_echo_in_prompt_is_trimmed(self):
        q = clean_quiz({"prompt": "下面哪个对？\nA. 数组已排序\nB. 数组接近有序",
                        "options": [{"label": "A", "body": "数组已排序"},
                                    {"label": "B", "body": "数组接近有序"}], "key": "A"},
                       outline([point()]))
        self.assertEqual(q["prompt"].strip(), "下面哪个对？")


class CleanAssessTest(unittest.TestCase):
    def test_marks_kind_and_id(self):
        a = clean_assess({"prompt": "用自己的话说说二分查找为什么要有序",
                          "key_points": ["有序", "折半"]}, outline([point(kind="concept")]))
        self.assertEqual(a["kind"], "assess")
        self.assertEqual(a["kpId"], "p1")
        self.assertTrue(a["questionId"].startswith("a-"))
        self.assertEqual(len(a["key_points"]), 2)

    def test_no_prompt_rejected(self):
        self.assertIsNone(clean_assess({}, outline([point()])))


class SessionTurnTest(unittest.TestCase):
    def reply(self, **extra):
        body = {"say": "这一段讲了划分。", "quiz": None, "assess": None}
        body.update(extra)
        return json.dumps(body)

    def payload(self, **extra):
        base = {
            "goal": "一周复习完线性代数", "subject": "计算机",
            "materials": [], "learner": {},
            "outline": outline([point()]),
            "pending": pending(diff=2), "now": 1000,
            "message": "B", "mode": "study", "messages": [],
        }
        base.update(extra)
        return base

    def test_answer_is_graded_before_the_model_runs(self):
        provider = FakeProvider([self.reply()])
        result = run_session_turn(provider, self.payload(answer=answer("B")),
                                  [], "study")
        self.assertTrue(result["grade"]["ok"])
        self.assertEqual(result["outline"]["modules"][0]["points"][0]["attempts"],
                         [attempt(True, 2, at=1000)])
        # 判分事实已经写进提示词
        prompt = provider.calls[0][0][1]["content"]
        self.assertIn("引擎判：正确", prompt)

    def test_wrong_answer_is_not_mastery(self):
        provider = FakeProvider([self.reply()])
        result = run_session_turn(provider, self.payload(answer=answer("C")),
                                  [], "study")
        self.assertFalse(result["grade"]["ok"])
        self.assertFalse(result["outline"]["modules"][0]["points"][0]["done"])

    def test_crossing_the_gate_is_reported_once(self):
        seeded = point(attempts=[attempt(True, 3)], done=False)
        body = self.payload(answer=answer("B"))
        body["outline"] = outline([seeded])
        result = run_session_turn(FakeProvider([self.reply()]), body, [], "study")
        self.assertIsNotNone(result.get("mastered"))
        self.assertEqual(result["mastered"]["id"], "p1")
        self.assertTrue(result["outline"]["modules"][0]["points"][0]["done"])

    def test_answer_for_other_question_is_ignored(self):
        provider = FakeProvider([self.reply()])
        result = run_session_turn(
            provider, self.payload(answer=answer("B", qid="q-old")),
            [], "study")
        self.assertIsNone(result["grade"])
        self.assertEqual(result["outline"]["modules"][0]["points"][0]["attempts"], [])

    def test_model_failure_still_keeps_the_grade(self):
        provider = FakeProvider(["不是 JSON", "还不是"])
        result = run_session_turn(provider, self.payload(answer=answer("B")),
                                  [], "study")
        self.assertTrue(result["error"])
        self.assertTrue(result["grade"]["ok"])
        self.assertIsNone(result["quiz"])

    def test_offline_grades_without_posing(self):
        result = run_session_turn(None, self.payload(answer=answer("B")), [], "study")
        self.assertTrue(result["offline"])
        self.assertTrue(result["grade"]["ok"])
        self.assertIsNone(result["quiz"])
        self.assertIn("离线演示", result["reply"])

    def test_offline_without_answer_explains_itself(self):
        result = run_session_turn(None, self.payload(pending=None), [], "review")
        self.assertTrue(result["offline"])
        self.assertIsNone(result["grade"])

    def test_quiz_comes_back_as_a_card(self):
        quiz = {"kpId": "p1", "prompt": "哪种表现说明哈希表退化了？",
                "options": [{"label": "A", "body": "冲突都堆在一条链上"},
                            {"label": "B", "body": "负载因子接近零"}],
                "key": "A", "explanation": "冲突集中时查找变线性。", "difficulty": 3}
        result = run_session_turn(
            FakeProvider([self.reply(quiz=quiz)]), self.payload(pending=None, answer=None),
            [], "study")
        self.assertEqual(result["quiz"]["kpId"], "p1")
        self.assertEqual(result["quiz"]["key"], "A")
        self.assertEqual(result["mode"], "study")


class RunTurnRoutingTest(unittest.TestCase):
    def test_outline_mode_when_no_outline(self):
        provider = FakeProvider([json.dumps({
            "say": "先定大纲。", "stage": "intake", "ask": {
                "intro": "先问四件事",
                "questions": [{"id": "prior", "header": "基础",
                               "prompt": "你现在会到什么程度？",
                               "options": [{"label": "零基础", "description": ""},
                                           {"label": "学过一遍", "description": ""}]}]},
            "outline": None})])
        result = run_turn(provider, {"message": "开始", "goal": "复习线代",
                                     "outline": {"modules": []}}, [])
        self.assertEqual(result["mode"], "outline")

    def test_study_mode_when_outline_exists(self):
        provider = FakeProvider([json.dumps({"say": "讲一段。", "quiz": None,
                                             "assess": None})])
        result = run_turn(provider, {"message": "继续", "goal": "复习线代",
                                     "outline": outline([point()]),
                                     "mode": "study"}, [])
        self.assertEqual(result["mode"], "study")
        self.assertIn("【当前模式】学习", provider.calls[0][0][1]["content"])

    def test_start_request_from_outline_switches_and_teaches(self):
        """大纲定稿后说「开始学习吧」= 模式请求：这一轮就要开讲。"""
        provider = FakeProvider([json.dumps({"say": "先讲第一段。", "quiz": None,
                                             "assess": None})])
        result = run_turn(provider, {"message": "开始学习吧", "goal": "复习线代",
                                     "outline": outline([point()]),
                                     "mode": "outline"}, [])
        self.assertEqual(result["mode"], "study")
        self.assertEqual(result.get("switched"), "study")
        self.assertIn("【当前模式】学习", provider.calls[0][0][1]["content"])
        self.assertIn("【这一轮该讲】", provider.calls[0][0][1]["content"])

    def test_outline_discussion_is_not_a_start_request(self):
        provider = FakeProvider([json.dumps({"say": "我把第二模块拆细。",
                                             "stage": "draft", "outline": None})])
        result = run_turn(provider, {"message": "第二模块拆细一点，再加一个实验环节",
                                     "goal": "复习线代",
                                     "outline": outline([point()]),
                                     "mode": "outline"}, [])
        self.assertEqual(result["mode"], "outline")
        self.assertNotIn("switched", result)

    def test_start_request_without_outline_stays_outline(self):
        provider = FakeProvider([json.dumps({
            "say": "先定大纲。", "stage": "intake", "ask": None, "outline": None})])
        result = run_turn(provider, {"message": "开始学习吧", "goal": "复习线代",
                                     "outline": {"modules": []},
                                     "mode": "outline"}, [])
        self.assertEqual(result["mode"], "outline")
        self.assertNotIn("switched", result)


class WantsStudyTest(unittest.TestCase):
    """只有很短的一句「开始学吧」才算指令，不能把正经讨论当请求。"""

    def test_plain_start_phrases(self):
        for text in ("开始学习吧", "好的，开始学", "那就开始上课吧", "继续学吧", "开始！"):
            self.assertTrue(wants_study(text), text)

    def test_discussion_is_not_a_command(self):
        for text in ("", "第二模块拆细一点，再加一个实验环节",
                     "开始之前先看看资料够不够？", "我想先换个目标"):
            self.assertFalse(wants_study(text), text)


class GuardTest(unittest.TestCase):
    """出题即结束这一轮：答应了出题却没出，要在重试后如实收尾。"""

    def payload(self):
        return {"goal": "一周复习完线性代数", "outline": outline([point()]),
                "pending": None, "answer": None, "now": 1000,
                "message": "开始学", "mode": "study", "messages": []}

    def good(self):
        return json.dumps({"say": "讲到这里，试一道。", "quiz": {
            "kpId": "p1", "prompt": "二分查找要求数组怎样？",
            "options": [{"label": "A", "body": "已排序"},
                        {"label": "B", "body": "完全逆序"}],
            "key": "A", "explanation": "折半依赖有序。", "difficulty": 2},
            "assess": None})

    def test_dangling_colon_is_retried(self):
        provider = FakeProvider([json.dumps({"say": "下面这道题：", "quiz": None,
                                             "assess": None}), self.good()])
        result = run_session_turn(provider, self.payload(), [], "study")
        self.assertIsNotNone(result["quiz"])
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("出题规则", provider.calls[1][0][-1]["content"])

    def test_falls_back_without_fabricating_a_question(self):
        bad = json.dumps({"say": "来看看你知不知道：", "quiz": None, "assess": None})
        result = run_session_turn(FakeProvider([bad, bad]), self.payload(), [], "study")
        self.assertTrue(result["guarded"])
        self.assertIsNone(result["quiz"])
        self.assertFalse(result.get("error"))
        self.assertIn("没能把题目补完整", result["reply"])
        self.assertFalse(result["reply"].rstrip().endswith("："))

    def test_prose_options_count_as_a_missing_quiz(self):
        bad = json.dumps({"say": "你认为哪个对？\nA. 数组已排序\nB. 数组接近有序",
                          "quiz": None, "assess": None})
        result = run_session_turn(FakeProvider([bad, bad]), self.payload(), [], "study")
        self.assertTrue(result["guarded"])

    def test_a_plain_explanation_is_not_flagged(self):
        provider = FakeProvider([json.dumps({"say": "这一段讲了划分的边界。",
                                             "quiz": None, "assess": None})])
        result = run_session_turn(provider, self.payload(), [], "study")
        self.assertFalse(result.get("guarded"))
        self.assertEqual(len(provider.calls), 1)


class ControlTest(unittest.TestCase):
    """跳过 / 修题 / 暂缓 / 恢复：都不给掌握分，证据原样保留。"""

    def payload(self, **extra):
        body = {"goal": "一周复习完线性代数", "outline": outline([point()]),
                "pending": pending(diff=2), "answer": None, "now": 1000,
                "message": "跳过", "mode": "study", "messages": []}
        body.update(extra)
        return body

    def reply(self):
        return json.dumps({"say": "换一道更小的：二分查找的第一步是？", "quiz": {
            "kpId": "p1", "prompt": "二分查找的第一步是？",
            "options": [{"label": "A", "body": "取中间元素比较"},
                        {"label": "B", "body": "从头遍历"}],
            "key": "A", "explanation": "先看中间。", "difficulty": 1},
            "assess": None})

    def test_skip_records_no_attempt(self):
        provider = FakeProvider([self.reply()])
        result = run_session_turn(
            provider, self.payload(control={"kind": "skip", "questionId": "q-1"}),
            [], "study")
        p = result["outline"]["modules"][0]["points"][0]
        self.assertEqual(p["attempts"], [])
        self.assertEqual(p["skips"], 1)
        self.assertIsNone(result["grade"])
        self.assertIn("跳过了这道题", provider.calls[0][0][1]["content"])
        self.assertIn("不要重出同一道题", provider.calls[0][0][1]["content"])

    def test_repair_counts_and_asks_for_a_fixed_question(self):
        provider = FakeProvider([self.reply()])
        result = run_session_turn(
            provider, self.payload(control={"kind": "repair", "note": "选项有错"}),
            [], "study")
        p = result["outline"]["modules"][0]["points"][0]
        self.assertEqual(p["repairs"], 1)
        self.assertEqual(p["attempts"], [])
        self.assertIn("这道题有问题", provider.calls[0][0][1]["content"])

    def test_defer_moves_the_target_forward(self):
        body = self.payload(control={"kind": "defer"})
        body["outline"] = outline([point(pid="p1", name="二分查找"),
                                   point(pid="p2", name="哈希表")])
        provider = FakeProvider([self.reply()])
        result = run_session_turn(provider, body, [], "study")
        first = result["outline"]["modules"][0]["points"][0]
        self.assertEqual(first["deferredAt"], 1000)
        self.assertEqual(first["attempts"], [])
        prompt = provider.calls[0][0][1]["content"]
        self.assertIn("先放一放", prompt)
        self.assertIn("哈希表", prompt)

    def test_resume_clears_the_deferral(self):
        body = self.payload(pending=None, control={"kind": "resume", "kpId": "p1"})
        body["outline"] = outline([point(pid="p1", name="二分查找")])
        body["outline"]["modules"][0]["points"][0]["deferredAt"] = 900
        provider = FakeProvider([self.reply()])
        result = run_session_turn(provider, body, [], "study")
        p = result["outline"]["modules"][0]["points"][0]
        self.assertEqual(p["deferredAt"], 0)
        self.assertIn("二分查找", provider.calls[0][0][1]["content"])

    def test_offline_control_still_explains_itself(self):
        result = run_session_turn(
            None, self.payload(control={"kind": "skip", "questionId": "q-1"}),
            [], "study")
        self.assertTrue(result["offline"])
        self.assertIsNone(result["grade"])
        self.assertIn("先跳过", result["reply"])


if __name__ == "__main__":
    unittest.main()
