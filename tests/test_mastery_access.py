# -*- coding: utf-8 -*-
"""精通之路读知识库也要过权限闸门：拿到库 id 不等于能读。"""
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from fastapi import HTTPException

    from api import app as app_module
    from api.app import _mastery_digest
except ImportError as exc:  # CI 只装标准库与文件解析依赖，不装 fastapi
    raise unittest.SkipTest("需要 fastapi 才能测 API 层权限闸门：%s" % exc)

from kb.library import Library


class FakeRequest:
    """只给权限判断需要的两个属性，不拉起整个 FastAPI。"""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.client = SimpleNamespace(host="127.0.0.1")


class MasteryDigestAccessTest(unittest.TestCase):
    def setUp(self):
        self.lib = Library(mode="bm25")
        self.private = self.lib.create_collection(
            "老板的私有库", owner_id="boss", visibility="private")
        self.shared = self.lib.create_collection("公司库", owner_id="boss")
        patcher = mock.patch.dict(
            app_module._state, {"library": self.lib}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def material(self, cid):
        return SimpleNamespace(key="collection:" + cid, kind="collection",
                               label="资料", detail="")

    def test_private_collection_is_rejected_without_login(self):
        with self.assertRaises(HTTPException) as ctx:
            _mastery_digest([self.material(self.private["id"])], FakeRequest())
        self.assertEqual(ctx.exception.status_code, 401)

    def test_shared_collection_still_needs_a_login(self):
        with self.assertRaises(HTTPException) as ctx:
            _mastery_digest([self.material(self.shared["id"])], FakeRequest())
        self.assertEqual(ctx.exception.status_code, 401)

    def test_unknown_collection_is_not_found(self):
        with self.assertRaises(HTTPException) as ctx:
            _mastery_digest([self.material("lib-does-not-exist")], FakeRequest())
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
