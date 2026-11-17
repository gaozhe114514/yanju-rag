# -*- coding: utf-8 -*-
"""版本信息：前后端共用一个 version.json。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api import version as version_module

try:
    from api import app as app_module
except ImportError:  # CI 不装 fastapi：端点一致性那条跳过，版本文件逻辑照测
    app_module = None


class VersionInfoTest(unittest.TestCase):
    def test_reads_repo_version_file(self):
        info = version_module.version_info()
        self.assertRegex(info["version"], r"^\d+\.\d+")
        self.assertTrue(info["released"])
        self.assertEqual(info["name"], "言据")
        self.assertIsInstance(info["history"], list)
        self.assertTrue(info["history"])
        first = info["history"][0]
        self.assertEqual(first["version"], info["version"])
        self.assertIn("title", first)

    @unittest.skipUnless(app_module, "需要 fastapi 才能导入 api.app")
    def test_health_and_version_endpoint_agree(self):
        current = app_module.version()["version"]
        self.assertEqual(app_module.health()["version"], current)
        # FastAPI 自己的版本号也从 version.json 来，不再手写第二份
        self.assertEqual(app_module.app.version, current)

    def test_frontend_meta_matches_version_file(self):
        # 页面的 app-version 是手写的，容易和后端 version.json 漂移
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        current = version_module.version_info()["version"]
        self.assertIn('<meta name="app-version" content="%s">' % current, html)

    def test_malformed_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch.object(version_module, "VERSION_FILE", path):
                info = version_module.version_info()
        self.assertEqual(info["version"], "0.0")
        self.assertEqual(info["history"], [])

    def test_history_capped_and_filtered(self):
        payload = {
            "version": "9.9",
            "history": [{"version": str(i)} for i in range(30)] + ["junk"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "version.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(version_module, "VERSION_FILE", path):
                info = version_module.version_info()
        self.assertEqual(info["version"], "9.9")
        self.assertEqual(len(info["history"]), 20)
        self.assertTrue(all(isinstance(item, dict) for item in info["history"]))


if __name__ == "__main__":
    unittest.main()
