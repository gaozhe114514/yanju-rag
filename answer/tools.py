# -*- coding: utf-8 -*-
"""日常模式的内置工具。

照 DeepTutor 的设置思路：工具是账号级资源，用户在设置里决定开哪些；
一次请求只带开着的工具名，服务端按白名单执行，前端不能临时点名任意外部地址。
目前只有天气（Open-Meteo，无需密钥），资料模式永远不调用这里的任何东西。
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 6

# 工具注册表：前端设置页按这里的 label / desc 渲染开关，服务端只认这些名字
TOOLS: Dict[str, Dict[str, str]] = {
    "weather": {
        "label": "天气查询",
        "desc": "问天气时取实时气温、今日与明日预报（Open-Meteo，无需密钥）。",
    },
}

# WMO 天气码 → 中文；模型只拿这句，不做二次推断
_WMO = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
}

# 从「今天郑州天气怎么样」里剥掉时间和问法，剩下城市名
_STOP_WORDS = [
    "今天", "明天", "后天", "现在", "当前", "刚才", "这几天", "最近",
    "请问", "帮我", "查一下", "查查", "查下", "看看", "看一下", "告诉我",
    "天气", "气温", "温度", "预报", "怎么样", "如何", "多少度", "几度",
    "冷不冷", "热不热", "会不会", "会下雨", "会下雪", "要下雨", "要下雪",
    "有雨", "有雪", "下雨", "下雪", "刮风",
    "的", "呢", "吗", "啊", "呀",
    "weather", "forecast", "today", "tomorrow", "now", "what", "what's",
    "is", "the", "in", "at", "how", "it", "like",
]
_PUNCT = re.compile(r"[。，、！？,.!?;；:：\"'“”‘’()（）\[\]【】\s]+")


def extract_city(query: str) -> str:
    """取出城市名；取不出就返回空串（调用方让工具说没听清，不猜）。"""
    text = _PUNCT.sub(" ", str(query or ""))
    for word in _STOP_WORDS:
        text = re.sub(re.escape(word), " ", text, flags=re.IGNORECASE)
    left = [p for p in text.split() if p]
    city = "".join(left).strip()
    if not city or len(city) > 12:
        return ""
    return city


def weather_label(code) -> str:
    try:
        return _WMO.get(int(code), "未知天气")
    except (TypeError, ValueError):
        return "未知天气"


def _get_json(url: str, params: Dict[str, str]) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "yinzheng-tools/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _num(value, digits: int = 0) -> str:
    try:
        return ("%." + str(digits) + "f") % float(value)
    except (TypeError, ValueError):
        return "—"


def weather_lookup(query: str, fetch: Callable[[str, Dict[str, str]], dict] = None) -> dict:
    """查天气。返回统一结构，失败也返回结构，不抛异常给上层。"""
    fetch = fetch or _get_json
    city = extract_city(query)
    base = {"name": "weather", "label": "天气", "source": "Open-Meteo",
            "detail": "", "ok": False}
    if not city:
        base["detail"] = "没听出要查哪个城市。说「郑州天气」这种格式，我再查。"
        return base
    try:
        geo = fetch(GEOCODE_URL, {"name": city, "count": "1", "language": "zh",
                                  "format": "json"})
        hits = (geo or {}).get("results") or []
        if not hits:
            base["detail"] = "Open-Meteo 里没找到「%s」这个地名。" % city
            return base
        place = hits[0]
        lat, lon = place.get("latitude"), place.get("longitude")
        data = fetch(FORECAST_URL, {
            "latitude": str(lat), "longitude": str(lon),
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,weather_code",
            "timezone": "auto", "forecast_days": "2",
        })
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        base["detail"] = "天气服务暂时连不上（%s）。" % type(e).__name__
        return base

    cur = (data or {}).get("current") or {}
    daily = (data or {}).get("daily") or {}
    temps = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []
    codes = daily.get("weather_code") or []
    name = place.get("name") or city
    region = place.get("admin1") or place.get("country") or ""

    parts = ["%s%s现在 %s℃，体感 %s℃，%s，湿度 %s%%，风速 %s km/h" % (
        name, ("（" + region + "）") if region else "",
        _num(cur.get("temperature_2m"), 1), _num(cur.get("apparent_temperature"), 1),
        weather_label(cur.get("weather_code")), _num(cur.get("relative_humidity_2m")),
        _num(cur.get("wind_speed_10m"), 1))]
    if temps and lows:
        today = "今天 %s–%s℃" % (_num(lows[0], 1), _num(temps[0], 1))
        if rains:
            today += "，降水概率 %s%%" % _num(rains[0])
        if codes:
            today += "，" + weather_label(codes[0])
        parts.append(today)
    if len(temps) > 1 and len(lows) > 1:
        tomorrow = "明天 %s–%s℃" % (_num(lows[1], 1), _num(temps[1], 1))
        if len(rains) > 1:
            tomorrow += "，降水概率 %s%%" % _num(rains[1])
        if len(codes) > 1:
            tomorrow += "，" + weather_label(codes[1])
        parts.append(tomorrow)
    stamp = cur.get("time") or ""
    tz = (data or {}).get("timezone") or ""
    parts.append("数据来自 Open-Meteo%s%s" % (
        "，" + tz if tz else "", "，" + stamp if stamp else ""))

    base["ok"] = True
    base["query"] = city
    base["detail"] = "；".join(parts) + "。"
    return base


_HANDLERS = {"weather": weather_lookup}

# 只有问法确实沾边才调用：开着天气工具问「排个计划」，不该白打一次外部接口
_WHEN = {
    "weather": re.compile(
        r"天气|气温|温度|下雨|下雪|降雨|降雪|降温|升温|预报|多少度|几度|"
        r"冷不冷|热不热|weather|forecast|temperature", re.IGNORECASE),
}


def run_tools(names: List[str], query: str) -> List[dict]:
    """按前端给的白名单逐个执行；未知名字或问法不沾边的直接跳过。"""
    out = []
    seen = set()
    for name in names or []:
        name = str(name or "").strip()
        if not name or name in seen or name not in _HANDLERS:
            continue
        when = _WHEN.get(name)
        if when is not None and not when.search(query or ""):
            continue
        seen.add(name)
        out.append(_HANDLERS[name](query))
        if len(out) >= 4:
            break
    return out


def tool_context(results: List[dict]) -> str:
    """把工具结果拼成一段 system 文本；没有结果返回空串。"""
    if not results:
        return ""
    lines = ["下面是工具刚刚返回的实时数据（不是用户资料，不需要逐字引用）："]
    for r in results:
        if r.get("ok"):
            lines.append("- %s：%s" % (r.get("label", "工具"), r.get("detail", "")))
        else:
            lines.append("- %s：调用失败。%s" % (r.get("label", "工具"),
                                              r.get("detail", "")))
    lines.append("回答时可以直接用这些数据，并说明来源与时间；工具失败就直说查不到，"
                 "不要编造，也不要说「根据资料」。")
    return "\n".join(lines)
