# -*- coding: utf-8 -*-
"""日常模式的内置工具：城市解析、天气格式化、失败不抛异常、白名单执行。"""
import unittest

from answer import tools
from answer.tools import (extract_city, run_tools, tool_context, weather_label,
                          weather_lookup)

GEO = {"results": [{"name": "郑州", "admin1": "河南",
                    "latitude": 34.75778, "longitude": 113.64861}]}
FORECAST = {
    "timezone": "Asia/Shanghai",
    "current": {"time": "2026-10-30T16:00", "temperature_2m": 21.3,
                "apparent_temperature": 20.1, "weather_code": 2,
                "relative_humidity_2m": 56, "wind_speed_10m": 8.3},
    "daily": {"temperature_2m_max": [26.4, 25.1],
              "temperature_2m_min": [18.2, 17.0],
              "precipitation_probability_max": [10, 60],
              "weather_code": [2, 61]},
}


class FakeFetch:
    def __init__(self, geo=None, forecast=None, error=None):
        self.geo = GEO if geo is None else geo
        self.forecast = FORECAST if forecast is None else forecast
        self.error = error
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        if self.error:
            raise self.error
        if url == tools.GEOCODE_URL:
            return self.geo
        return self.forecast


class ExtractCityTest(unittest.TestCase):
    def test_chinese_question(self):
        self.assertEqual(extract_city("今天郑州天气怎么样"), "郑州")
        self.assertEqual(extract_city("北京明天会下雨吗"), "北京")
        self.assertEqual(extract_city("查一下上海的天气"), "上海")

    def test_english_question(self):
        self.assertEqual(extract_city("Zhengzhou weather today"), "Zhengzhou")

    def test_no_city_left(self):
        self.assertEqual(extract_city("今天天气怎么样"), "")
        self.assertEqual(extract_city(""), "")

    def test_overlong_is_rejected(self):
        self.assertEqual(extract_city("这是一个非常非常非常非常长的地名啊啊啊"), "")


class WeatherLabelTest(unittest.TestCase):
    def test_known_codes(self):
        self.assertEqual(weather_label(0), "晴")
        self.assertEqual(weather_label(61), "小雨")
        self.assertEqual(weather_label(95), "雷暴")

    def test_unknown_and_bad_values(self):
        self.assertEqual(weather_label(999), "未知天气")
        self.assertEqual(weather_label(None), "未知天气")


class WeatherLookupTest(unittest.TestCase):
    def test_success_summary(self):
        fetch = FakeFetch()
        res = weather_lookup("今天郑州天气怎么样", fetch=fetch)
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "Open-Meteo")
        self.assertIn("郑州（河南）现在 21.3℃", res["detail"])
        self.assertIn("今天 18.2–26.4℃", res["detail"])
        self.assertIn("明天 17.0–25.1℃", res["detail"])
        self.assertIn("小雨", res["detail"])
        self.assertIn("降水概率 60%", res["detail"])
        self.assertIn("Asia/Shanghai", res["detail"])
        # 地理编码用中文，天气预报用坐标
        self.assertEqual(fetch.calls[0][1]["name"], "郑州")
        self.assertEqual(fetch.calls[1][1]["latitude"], "34.75778")

    def test_no_city_is_a_polite_failure(self):
        res = weather_lookup("今天天气怎么样", fetch=FakeFetch())
        self.assertFalse(res["ok"])
        self.assertIn("哪个城市", res["detail"])

    def test_unknown_place(self):
        res = weather_lookup("火星天气", fetch=FakeFetch(geo={"results": []}))
        self.assertFalse(res["ok"])
        self.assertIn("没找到", res["detail"])

    def test_network_error_does_not_raise(self):
        res = weather_lookup("郑州天气", fetch=FakeFetch(error=OSError("down")))
        self.assertFalse(res["ok"])
        self.assertIn("连不上", res["detail"])


class RunToolsTest(unittest.TestCase):
    def test_unknown_names_are_ignored(self):
        self.assertEqual(run_tools(["rm_rf", ""], "郑州天气"), [])

    def test_unrelated_query_skips_the_network(self):
        # 「排个计划」不该被当成地名去查天气
        calls = []
        old = tools._HANDLERS["weather"]
        tools._HANDLERS["weather"] = lambda q: calls.append(q) or {"ok": True}
        try:
            self.assertEqual(run_tools(["weather"], "帮我排个学习计划"), [])
            self.assertEqual(run_tools(["weather"], "郑州今天多少度"), [{"ok": True}])
        finally:
            tools._HANDLERS["weather"] = old
        self.assertEqual(calls, ["郑州今天多少度"])

    def test_weather_runs_once(self):
        # 这里不碰网络：把 handler 临时换掉
        old = tools._HANDLERS["weather"]
        calls = []

        def fake(query):
            calls.append(query)
            return {"name": "weather", "label": "天气", "ok": True,
                    "detail": "晴", "source": "Open-Meteo"}

        tools._HANDLERS["weather"] = fake
        try:
            out = run_tools(["weather", "weather", "nope"], "郑州天气")
        finally:
            tools._HANDLERS["weather"] = old
        self.assertEqual(len(out), 1)
        self.assertEqual(calls, ["郑州天气"])


class ToolContextTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(tool_context([]), "")

    def test_success_and_failure_are_both_spelled_out(self):
        text = tool_context([
            {"label": "天气", "ok": True, "detail": "郑州晴", "source": "Open-Meteo"},
            {"label": "天气", "ok": False, "detail": "连不上"},
        ])
        self.assertIn("郑州晴", text)
        self.assertIn("调用失败", text)
        self.assertIn("不要编造", text)
        self.assertIn("不是用户资料", text)


if __name__ == "__main__":
    unittest.main()
