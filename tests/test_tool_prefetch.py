# -*- coding: utf-8 -*-
"""工具并行预取：实时数据判定与取结果时的超时 / 出错处理。"""
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from api.tools_runtime import _live_query, _split_tools, _tool_results


class SplitToolsTest(unittest.TestCase):
    def test_only_local_or_cheap_tools_prefetch(self):
        fast, slow = _split_tools(["weather", "search", "calc", "", "fetch"])
        self.assertEqual(fast, ["weather", "calc"])
        self.assertEqual(slow, ["search", "fetch"])

    def test_nothing_enabled(self):
        self.assertEqual(_split_tools(None), ([], []))


class LiveQueryTest(unittest.TestCase):
    def test_live_data_question(self):
        self.assertTrue(_live_query(["weather"], "今天开封市天气什么样"))
        self.assertTrue(_live_query(["time"], "现在几点"))
        self.assertTrue(_live_query(["calc"], "12 * 34 等于多少"))

    def test_kb_question_is_not_live(self):
        # 开着天气工具问 408，也不能当成实时数据问题跳过精排
        self.assertFalse(_live_query(["weather"], "TCP 三次握手为什么不能是两次"))
        self.assertFalse(_live_query(["study"], "今天几号"))
        self.assertFalse(_live_query(None, "今天几号"))


class ToolResultsTest(unittest.TestCase):
    def setUp(self):
        self.pool = ThreadPoolExecutor(max_workers=2)

    def tearDown(self):
        self.pool.shutdown(wait=True)

    def test_no_future(self):
        self.assertEqual(_tool_results(None), [])

    def test_completed_result(self):
        future = self.pool.submit(lambda: [{"label": "天气", "ok": True}])
        self.assertEqual(_tool_results(future)[0]["label"], "天气")

    def test_timeout_says_so(self):
        future = self.pool.submit(time.sleep, 0.3)
        out = _tool_results(future, timeout=0.01)
        self.assertFalse(out[0]["ok"])
        self.assertIn("没在规定时间内", out[0]["detail"])
        future.result()

    def test_error_is_reported(self):
        def boom():
            raise RuntimeError("x")

        out = _tool_results(self.pool.submit(boom))
        self.assertFalse(out[0]["ok"])
        self.assertIn("调用出错", out[0]["detail"])


if __name__ == "__main__":
    unittest.main()
