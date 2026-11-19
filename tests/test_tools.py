# -*- coding: utf-8 -*-
"""日常模式的内置工具：城市解析、天气格式化、失败不抛异常、白名单执行。"""
import unittest

from answer import tools
from answer.tools import (calc_lookup, code_lookup, extract_city, run_tools,
                          time_lookup, tool_context, tool_will_run,
                          weather_label, weather_lookup, web_fetch, web_search)
from answer.tools import _search_terms

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

WTTR = {
    "current_condition": [{"temp_C": "14", "FeelsLikeC": "11",
                           "humidity": "49", "windspeedKmph": "9",
                           "weatherDesc": [{"value": "Cloudy"}]}],
    "weather": [
        {"date": "2026-11-19", "maxtempC": "16", "mintempC": "11",
         "hourly": [{"chanceofrain": "100", "weatherDesc": [{"value": "Light rain"}]}]},
        {"date": "2026-11-20", "maxtempC": "15", "mintempC": "9",
         "hourly": [{"chanceofrain": "20", "weatherDesc": [{"value": "Sunny"}]}]},
    ],
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


class FallbackFetch(FakeFetch):
    """地理编码正常，Open-Meteo 预报挂掉，wttr.in 正常。"""

    def __call__(self, url, params):
        self.calls.append((url, params))
        if url == tools.GEOCODE_URL:
            return self.geo
        if url == tools.FORECAST_URL:
            raise OSError("forecast down")
        return WTTR


class ExtractCityTest(unittest.TestCase):
    def test_chinese_question(self):
        self.assertEqual(extract_city("今天郑州天气怎么样"), "郑州")
        self.assertEqual(extract_city("北京明天会下雨吗"), "北京")
        self.assertEqual(extract_city("查一下上海的天气"), "上海")

    def test_trailing_question_words(self):
        # v0.57：线上问过「今天开封市天气什么样」，城市被连问法一起取走，
        # Open-Meteo 查不到「开封市什么样」这个地名，天气工具直接失败。
        self.assertEqual(extract_city("今天开封市天气什么样"), "开封市")
        self.assertEqual(extract_city("北京啥样"), "北京")
        self.assertEqual(extract_city("开封市区的天气怎么样"), "开封市")

    def test_english_question(self):
        self.assertEqual(extract_city("Zhengzhou weather today"), "Zhengzhou")

    def test_no_city_left(self):
        self.assertEqual(extract_city("今天天气怎么样"), "")
        self.assertEqual(extract_city(""), "")

    def test_overlong_is_rejected(self):
        self.assertEqual(extract_city("这是一个非常非常非常非常长的地名啊啊啊"), "")


class GeocodeCacheTest(unittest.TestCase):
    def setUp(self):
        tools._GEO_CACHE.clear()

    def tearDown(self):
        tools._GEO_CACHE.clear()

    def test_second_call_hits_cache(self):
        fetch = FakeFetch()
        first = tools._geocode("郑州", fetch, cache=True)
        second = tools._geocode("郑州 ", fetch, cache=True)
        self.assertEqual(first, second)
        self.assertEqual(len(fetch.calls), 1)

    def test_cache_can_be_turned_off(self):
        fetch = FakeFetch()
        tools._geocode("郑州", fetch, cache=False)
        tools._geocode("郑州", fetch, cache=False)
        self.assertEqual(len(fetch.calls), 2)


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

    def test_open_meteo_forecast_down_falls_back_to_wttr(self):
        res = weather_lookup("郑州天气", fetch=FallbackFetch())
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "wttr.in")
        self.assertIn("郑州现在 14℃", res["detail"])
        self.assertIn("今天 11–16℃", res["detail"])
        self.assertIn("小雨", res["detail"])
        self.assertIn("wttr.in", res["detail"])

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
        tools._HANDLERS["weather"] = lambda q, context=None: calls.append(q) or {"ok": True}
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

        def fake(query, context=None):
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


class TimeToolTest(unittest.TestCase):
    def test_reports_china_time_and_target_days(self):
        res = time_lookup("今天几号", {"exam_days": 51})
        self.assertTrue(res["ok"])
        self.assertIn("Asia/Shanghai", res["detail"])
        self.assertIn("星期", res["detail"])
        self.assertIn("目标日", res["detail"])
        self.assertIn("51 天", res["detail"])

    def test_no_target_day_no_countdown(self):
        res = time_lookup("今天几号", {"exam_days": None})
        self.assertNotIn("目标日", res["detail"])


class CalcToolTest(unittest.TestCase):
    def test_arithmetic(self):
        self.assertTrue(calc_lookup("1+2×3 等于多少")["ok"])
        self.assertIn("= 7", calc_lookup("1+2×3 等于多少")["detail"])

    def test_percent(self):
        res = calc_lookup("200 的 15% 是多少")
        self.assertTrue(res["ok"])
        self.assertIn("30", res["detail"])

    def test_power_and_root(self):
        self.assertIn("81", calc_lookup("9 的平方")["detail"])
        self.assertIn("4", calc_lookup("根号 16")["detail"])

    def test_unit_conversion(self):
        res = calc_lookup("180cm 是多少米")
        self.assertTrue(res["ok"])
        self.assertIn("1.8", res["detail"])
        self.assertIn("长度换算", res["detail"])
        temp = calc_lookup("100 摄氏度是多少华氏度")
        self.assertTrue(temp["ok"])
        self.assertIn("212", temp["detail"])

    def test_mismatched_units(self):
        res = calc_lookup("180cm 是多少千克")
        self.assertFalse(res["ok"])
        self.assertIn("不是同一类", res["detail"])

    def test_bad_expression_does_not_raise(self):
        self.assertFalse(calc_lookup("1/0")["ok"])
        self.assertFalse(calc_lookup("__import__('os')")["ok"])
        self.assertFalse(calc_lookup("什么也看不出来")["ok"])

    def test_date_is_not_a_subtraction(self):
        # 2026-10-30 里的连字符不是减号，别算成 1986
        self.assertFalse(calc_lookup("2026-10-30 是星期几")["ok"])
        self.assertFalse(calc_lookup("12月25日还有几天")["ok"])
        self.assertFalse(tool_will_run(["calc"], "2026-10-30 是星期几"))
        self.assertTrue(tool_will_run(["time"], "2026-10-30 是星期几"))

    def test_bare_number_is_not_an_expression(self):
        self.assertFalse(calc_lookup("今日 30")["ok"])


class CodeToolTest(unittest.TestCase):
    def test_base_conversion_and_bits(self):
        self.assertIn("0b101010", code_lookup("bin(42)")["detail"])
        self.assertIn("15", code_lookup("0b1010 | 0b0101 是多少")["detail"])
        self.assertIn("8", code_lookup("1 << 3 等于多少")["detail"])

    def test_comprehension_and_builtins(self):
        res = code_lookup("sum(i*i for i in range(5))")
        self.assertTrue(res["ok"])
        self.assertIn("30", res["detail"])
        self.assertIn("'abc'", code_lookup("''.join(['a','b','c'])")["detail"])

    def test_imports_and_files_are_refused(self):
        for expr in ("__import__('os').system('dir')",
                     "open('x','w')",
                     "print(1)",
                     "().__class__",
                     "sum(i for i in range(100000000))"):
            res = code_lookup(expr)
            self.assertFalse(res["ok"], expr)

    def test_gate_keeps_calc_and_code_apart(self):
        self.assertTrue(tool_will_run(["code"], "bin(42) 是多少"))
        self.assertFalse(tool_will_run(["calc"], "bin(42) 是多少"))
        self.assertTrue(tool_will_run(["calc"], "12*8 等于多少"))
        self.assertFalse(tool_will_run(["code"], "今天几号"))


class SearchCueTest(unittest.TestCase):
    def test_official_site_question_fires_search(self):
        self.assertTrue(tool_will_run(["search"], "你能从官网查到吗"))

    def test_search_terms_drop_politeness_and_site_words(self):
        term = _search_terms("泛微网络是什么类型的公司 你能从官网查到吗")
        self.assertEqual(term, "泛微网络是什么类型的公司")


RSS = """<?xml version="1.0" encoding="utf-8"?><rss><channel>
<item><title>DeepSeek Harness</title><link>https://example.com/a</link>
<description>一切皆插件</description></item>
<item><title>第二条</title><link>https://example.com/b</link>
<description>&lt;b&gt;摘要&lt;/b&gt;</description></item>
</channel></rss>"""


class SearchToolTest(unittest.TestCase):
    def test_terms_are_stripped(self):
        self.assertEqual(tools._search_terms("帮我搜一下 2026 考研时间"), "2026 考研时间")

    def test_bing_rss_is_parsed(self):
        hits = tools._parse_bing_rss(RSS)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["url"], "https://example.com/a")
        self.assertEqual(hits[1]["snippet"], "摘要")

    def test_search_uses_bing_when_no_key(self):
        old_text, old_cfg = tools._get_text, tools._search_config
        tools._get_text = lambda *a, **k: RSS
        tools._search_config = lambda: ("", "")
        try:
            res = web_search("搜一下 DeepSeek Harness")
        finally:
            tools._get_text, tools._search_config = old_text, old_cfg
        self.assertTrue(res["ok"])
        self.assertEqual(res["source"], "Bing RSS")
        self.assertIn("https://example.com/a", res["detail"])
        self.assertTrue(res["external"])

    def test_missing_key_for_configured_provider(self):
        old_cfg = tools._search_config
        tools._search_config = lambda: ("tavily", "")
        try:
            res = web_search("搜一下 新闻")
        finally:
            tools._search_config = old_cfg
        self.assertFalse(res["ok"])
        self.assertIn("没配密钥", res["detail"])


class FetchToolTest(unittest.TestCase):
    def test_rejects_non_public_urls(self):
        self.assertFalse(tools._url_allowed("file:///etc/passwd"))
        self.assertFalse(tools._url_allowed("http://localhost:8000/health"))
        self.assertFalse(tools._url_allowed(
            "http://foo.example/", resolve=lambda *a, **k: [
                (2, 1, 6, "", ("127.0.0.1", 80))]))
        self.assertFalse(tools._url_allowed(
            "http://foo.example/", resolve=lambda *a, **k: [
                (2, 1, 6, "", ("169.254.169.254", 80))]))
        self.assertTrue(tools._url_allowed(
            "https://example.com/", resolve=lambda *a, **k: [
                (2, 1, 6, "", ("93.184.216.34", 443))]))

    def test_no_url_is_a_polite_failure(self):
        res = web_fetch("帮我看看这个页面")
        self.assertFalse(res["ok"])
        self.assertIn("链接", res["detail"])


class ToolGateTest(unittest.TestCase):
    def test_will_run_only_for_matching_query(self):
        self.assertTrue(tool_will_run(["time"], "今天几号"))
        self.assertFalse(tool_will_run(["time"], "帮我排个学习计划"))
        self.assertTrue(tool_will_run(["calc"], "12*8 等于多少"))
        self.assertTrue(tool_will_run(["fetch"], "看看 https://example.com/a"))
        self.assertFalse(tool_will_run([], "今天几号"))


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
