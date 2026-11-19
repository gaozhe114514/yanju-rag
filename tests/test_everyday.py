# -*- coding: utf-8 -*-
"""日常小工具：单位换算与注册表；外部接口只测纯逻辑。"""
import unittest

from answer.everyday import _IPV4_RE, _find_units, convert_lookup
from answer.tools import TOOLS, tool_will_run


class ConvertTest(unittest.TestCase):
    def test_length(self):
        out = convert_lookup("5 公里等于多少英里")
        self.assertTrue(out["ok"])
        self.assertIn("3.107", out["detail"])

    def test_data_uses_longest_unit(self):
        self.assertEqual(_find_units("10GB等于多少MB"), ["GB", "MB"])
        out = convert_lookup("10GB等于多少MB")
        self.assertTrue(out["ok"])
        self.assertIn("1.024e+04", out["detail"])

    def test_temperature(self):
        out = convert_lookup("100华氏度是多少摄氏度")
        self.assertTrue(out["ok"])
        self.assertIn("37.78", out["detail"])

    def test_mismatched_units_are_rejected(self):
        out = convert_lookup("5 公里等于多少公斤")
        self.assertFalse(out["ok"])


class EverydayRegistrationTest(unittest.TestCase):
    def test_tools_are_registered_and_fire(self):
        for key, q in [
            ("exchange", "美元兑人民币汇率"),
            ("air", "上海空气质量"),
            ("ip", "8.8.8.8 归属地"),
            ("convert", "5 公里等于多少英里"),
        ]:
            self.assertIn(key, TOOLS)
            self.assertTrue(tool_will_run([key], q), key)

    def test_ip_extracts_target(self):
        self.assertEqual(_IPV4_RE.search("8.8.8.8 归属地").group(0), "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
