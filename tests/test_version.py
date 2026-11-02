# -*- coding: utf-8 -*-
"""版本信息：前后端共用一个 version.json。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from api import app as app_module


class VersionInfoTest(unittest.TestCase):
    def test_reads_repo_version_file(self):
        info = app_module.version_info()
        self.assertRegex(info["version"], r"^\d+\.\d+")
        self.assertTrue(info["released"])
        self.assertEqual(info["name"], "言据")
        self.assertIsInstance(info["history"], list)
        self.assertTrue(info["history"])
        first = info["history"][0]
        self.assertEqual(first["version"], info["version"])
        self.assertIn("title", first)

    def test_health_and_version_endpoint_agree(self):
        current = app_module.version()["version"]
        self.assertEqual(app_module.health()["version"], current)
        # FastAPI 自己的版本号也从 version.json 来，不再手写第二份
        self.assertEqual(app_module.app.version, current)

    def test_malformed_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "version.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with mock.patch.object(app_module, "VERSION_FILE", app_module.Path(path)):
                info = app_module.version_info()
        self.assertEqual(info["version"], "0.0")
        self.assertEqual(info["history"], [])

    def test_history_capped_and_filtered(self):
        payload = {
            "version": "9.9",
            "history": [{"version": str(i)} for i in range(30)] + ["junk"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "version.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            with mock.patch.object(app_module, "VERSION_FILE", app_module.Path(path)):
                info = app_module.version_info()
        self.assertEqual(info["version"], "9.9")
        self.assertEqual(len(info["history"]), 20)
        self.assertTrue(all(isinstance(item, dict) for item in info["history"]))


if __name__ == "__main__":
    unittest.main()
