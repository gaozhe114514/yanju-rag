# -*- coding: utf-8 -*-
"""日常小工具：汇率、空气质量、IP / 域名归属、单位换算。

都走免费公开接口，不需要密钥；接口失败返回 ok=False，不抛异常。
"""
import re
import urllib.error
import urllib.parse
from typing import Dict, Optional

from . import tools as base

AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FX_URL = "https://hq.sinajs.cn/list="
IP_LOOKUP_URL = "https://ipwho.is/"

_FX_NAMES = {
    "人民币": "CNY", "美元": "USD", "美金": "USD", "欧元": "EUR",
    "日元": "JPY", "日圆": "JPY", "港币": "HKD", "港元": "HKD",
    "英镑": "GBP", "澳元": "AUD", "加元": "CAD", "瑞郎": "CHF",
    "新加坡元": "SGD", "新元": "SGD",
}
_FX_LABEL = {v: k for k, v in _FX_NAMES.items()}
_FX_LABEL.setdefault("CNY", "人民币")


def exchange_lookup(query: str, context: Optional[dict] = None) -> dict:
    out = {"name": "exchange", "label": "汇率换算", "source": "新浪财经",
           "detail": "", "ok": False}
    text = str(query or "")
    found = []
    for name, code in _FX_NAMES.items():
        if name in text:
            found.append((text.index(name), code))
    found.sort()
    codes = []
    for _, code in found:
        if code not in codes:
            codes.append(code)
    if not codes:
        out["detail"] = "没听出要换算哪两种货币，例如「美元兑人民币汇率」。"
        return out
    if len(codes) == 1:
        codes = ["CNY", codes[0]] if codes[0] != "CNY" else ["USD", "CNY"]
    src, dst = codes[0], codes[1]
    fields = []
    invert = False
    try:
        text_resp = base._get_text(
            FX_URL + "fx_s" + (src + dst).lower(),
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=base.TIMEOUT_S)
        match = re.search(r'="([^"]+)"', text_resp)
        if not match or not match.group(1).strip():
            invert = True
            text_resp = base._get_text(
                FX_URL + "fx_s" + (dst + src).lower(),
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=base.TIMEOUT_S)
            match = re.search(r'="([^"]+)"', text_resp)
        fields = match.group(1).split(",") if match else []
        rate = float(fields[1]) if len(fields) > 1 else 0.0
        if invert and rate:
            rate = 1.0 / rate
        if not rate:
            raise ValueError("empty")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            IndexError) as exc:
        out["detail"] = "汇率接口暂时连不上（%s）。" % type(exc).__name__
        return out
    out.update({
        "ok": True,
        "query": "%s/%s" % (src, dst),
        "detail": "1 %s ≈ %.4f %s；最近更新时间 %s。" % (
            _FX_LABEL.get(src, src), rate, _FX_LABEL.get(dst, dst),
            fields[0] if fields else "未知"),
    })
    return out


def air_lookup(query: str, fetch=None, context: Optional[dict] = None) -> dict:
    text = re.sub(r"(空气质量|空气指数|雾霾|AQI|PM2\.5|PM10|怎么样|如何|今天|现在)",
                  " ", str(query or ""))
    city = base.extract_city(text)
    out = {"name": "air", "label": "空气质量", "source": "Open-Meteo",
           "detail": "", "ok": False}
    if not city:
        out["detail"] = "没听出要查哪个城市，例如「上海空气质量」。"
        return out
    fetch = fetch or base._get_json
    try:
        geo = base._geocode(city, fetch, cache=fetch is base._get_json)
        hits = (geo or {}).get("results") or []
        if not hits:
            out["detail"] = "Open-Meteo 里没找到「%s」这个地名。" % city
            return out
        place = hits[0]
        data = fetch(AIR_QUALITY_URL, {
            "latitude": str(place["latitude"]),
            "longitude": str(place["longitude"]),
            "current": "pm2_5,pm10,us_aqi",
            "timezone": "Asia/Shanghai",
        })
        cur = (data or {}).get("current") or {}
        aqi = cur.get("us_aqi")
        if aqi is None:
            raise ValueError("no aqi")
        level = ("优" if aqi <= 50 else "良" if aqi <= 100 else
                 "轻度污染" if aqi <= 150 else "中度污染" if aqi <= 200 else
                 "重度污染" if aqi <= 300 else "严重污染")
        out.update({
            "ok": True,
            "detail": "%s当前 AQI %s（%s），PM2.5 %s μg/m³，PM10 %s μg/m³。"
                      % (place.get("name") or city, aqi, level,
                         cur.get("pm2_5", "—"), cur.get("pm10", "—")),
        })
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, TypeError) as exc:
        out["detail"] = "空气质量接口暂时连不上（%s）。" % type(exc).__name__
    return out


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN_RE = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b")


def ip_lookup(query: str, context: Optional[dict] = None) -> dict:
    out = {"name": "ip", "label": "IP / 域名归属", "source": "ipwho.is",
           "detail": "", "ok": False}
    text = str(query or "")
    match = _IPV4_RE.search(text) or _DOMAIN_RE.search(text)
    if not match:
        out["detail"] = "没找到 IP 或域名，例如「8.8.8.8 的归属地」。"
        return out
    target = match.group(0)
    try:
        data = base._get_json(IP_LOOKUP_URL + urllib.parse.quote(target), {})
        if not (data or {}).get("success", True):
            raise ValueError("not found")
        conn = data.get("connection") or {}
        city = "、".join(x for x in (data.get("country"), data.get("region"),
                                    data.get("city")) if x)
        out.update({
            "ok": True,
            "query": target,
            "detail": "%s 位于 %s；运营商 %s。"
                      % (data.get("ip") or target, city or "未知",
                         conn.get("isp") or conn.get("org") or "未知"),
        })
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, TypeError) as exc:
        out["detail"] = "IP 归属接口暂时连不上（%s）。" % type(exc).__name__
    return out


_UNITS = {
    "毫米": ("length", 0.001), "mm": ("length", 0.001),
    "厘米": ("length", 0.01), "cm": ("length", 0.01),
    "米": ("length", 1.0), "m": ("length", 1.0),
    "千米": ("length", 1000.0), "公里": ("length", 1000.0),
    "km": ("length", 1000.0),
    "英里": ("length", 1609.344), "mile": ("length", 1609.344),
    "英尺": ("length", 0.3048), "ft": ("length", 0.3048),
    "英寸": ("length", 0.0254), "inch": ("length", 0.0254),
    "克": ("weight", 0.001), "g": ("weight", 0.001),
    "千克": ("weight", 1.0), "公斤": ("weight", 1.0), "kg": ("weight", 1.0),
    "斤": ("weight", 0.5), "吨": ("weight", 1000.0), "t": ("weight", 1000.0),
    "磅": ("weight", 0.45359237), "lb": ("weight", 0.45359237),
    "B": ("data", 1.0), "KB": ("data", 1024.0), "MB": ("data", 1024.0 ** 2),
    "GB": ("data", 1024.0 ** 3), "TB": ("data", 1024.0 ** 4),
}
_TEMP_UNITS = {"摄氏度": "C", "℃": "C", "华氏度": "F", "℉": "F",
               "开尔文": "K", "K": "K"}


def _find_units(text: str):
    """按出现位置取单位，同一位置优先长单位（GB 不要被 B 抢先）。"""
    hits = []
    for unit in _UNITS:
        pos = text.find(unit)
        while pos >= 0:
            hits.append((pos, -len(unit), unit))
            pos = text.find(unit, pos + 1)
    hits.sort()
    chosen = []
    spans = []
    for pos, neg_len, unit in hits:
        end = pos + len(unit)
        if any(not (end <= a or pos >= b) for a, b in spans):
            continue
        chosen.append(unit)
        spans.append((pos, end))
    return chosen


def convert_lookup(query: str, context: Optional[dict] = None) -> dict:
    out = {"name": "convert", "label": "单位换算", "source": "本地计算",
           "detail": "", "ok": False}
    text = str(query or "")
    num = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not num:
        out["detail"] = "没找到要换算的数值，例如「5 公里等于多少英里」。"
        return out
    value = float(num.group(1))
    temp_hits = [(text.index(u), u) for u in _TEMP_UNITS if u in text]
    src_temp = min(temp_hits)[1] if temp_hits else ""
    if src_temp:
        rest = text[text.index(src_temp) + len(src_temp):]
        dst_temp = next((u for u in _TEMP_UNITS if u in rest), "")
        if not dst_temp:
            out["detail"] = "没找到目标温度单位，例如「100 华氏度是多少摄氏度」。"
            return out
        c = value
        if _TEMP_UNITS[src_temp] == "F":
            c = (value - 32) * 5 / 9
        elif _TEMP_UNITS[src_temp] == "K":
            c = value - 273.15
        result = c
        if _TEMP_UNITS[dst_temp] == "F":
            result = c * 9 / 5 + 32
        elif _TEMP_UNITS[dst_temp] == "K":
            result = c + 273.15
        out.update({"ok": True, "detail": "%s %s = %.2f %s。"
                    % (value, src_temp, result, dst_temp)})
        return out
    units = _find_units(text)
    if len(units) < 2:
        out["detail"] = "没找到两个单位，例如「5 公里等于多少英里」。"
        return out
    src, dst = units[0], units[1]
    if _UNITS[src][0] != _UNITS[dst][0]:
        out["detail"] = "「%s」和「%s」不是同一类单位，不能直接换算。" % (src, dst)
        return out
    result = value * _UNITS[src][1] / _UNITS[dst][1]
    out.update({"ok": True, "detail": "%s %s = %.4g %s。"
                % (value, src, result, dst)})
    return out
