# -*- coding: utf-8 -*-
"""知识库权限（B-lite）的纯函数测试，不需要数据库。"""
import unittest

from api.access import (ADMIN_ROLE, MEMBER_ROLE, can_manage, can_read,
                        can_write, describe, is_admin, role_label,
                        visibility_of)


ADMIN = {"id": "u-admin", "username": "admin", "role": ADMIN_ROLE}
MEMBER = {"id": "u-member", "username": "member", "role": MEMBER_ROLE}
OTHER = {"id": "u-other", "username": "other", "role": MEMBER_ROLE}


def coll(**kw):
    base = {"id": "lib-1", "name": "库", "owner_id": "", "visibility": "shared"}
    base.update(kw)
    return base


class BuiltinTest(unittest.TestCase):
    def test_everyone_can_read_nobody_can_write(self):
        preset = {"id": "cs408", "builtin": True}
        for user in (None, MEMBER, ADMIN):
            self.assertTrue(can_read(user, preset))
            self.assertFalse(can_write(user, preset))
        self.assertEqual(visibility_of(preset), "builtin")


class PrivateTest(unittest.TestCase):
    def test_only_owner_reads_and_writes(self):
        private = coll(visibility="private", owner_id=MEMBER["id"])
        self.assertTrue(can_read(MEMBER, private))
        self.assertTrue(can_write(MEMBER, private))
        # 个人库就是个人库，管理员也不能看
        self.assertFalse(can_read(ADMIN, private))
        self.assertFalse(can_write(ADMIN, private))
        self.assertFalse(can_read(OTHER, private))
        self.assertFalse(can_read(None, private))

    def test_wrong_owner_is_hidden(self):
        private = coll(visibility="private", owner_id="u-someone")
        self.assertFalse(can_read(MEMBER, private))
        self.assertEqual(role_label(MEMBER, private), "")


class SharedTest(unittest.TestCase):
    def test_logged_in_members_can_read(self):
        shared = coll()
        self.assertTrue(can_read(MEMBER, shared))
        self.assertFalse(can_read(None, shared))

    def test_admin_and_owner_can_write(self):
        shared = coll(owner_id=MEMBER["id"])
        self.assertTrue(can_write(ADMIN, shared))
        self.assertTrue(can_write(MEMBER, shared))
        self.assertFalse(can_write(OTHER, shared))

    def test_assigned_editor_can_write_viewer_cannot(self):
        shared = coll(owner_id=MEMBER["id"])
        self.assertTrue(can_write(OTHER, shared, "editor"))
        self.assertFalse(can_write(OTHER, shared, "viewer"))
        self.assertTrue(can_read(OTHER, shared, "viewer"))

    def test_legacy_shared_without_owner_is_admin_only_to_write(self):
        legacy = {"id": "lib-old", "name": "老库"}
        self.assertEqual(visibility_of(legacy), "shared")
        self.assertTrue(can_read(MEMBER, legacy))
        self.assertFalse(can_write(MEMBER, legacy))
        self.assertTrue(can_write(ADMIN, legacy))


class WeknoraTest(unittest.TestCase):
    def test_admin_only(self):
        wk = coll(weknora=True)
        self.assertTrue(can_read(ADMIN, wk))
        self.assertTrue(can_write(ADMIN, wk))
        self.assertFalse(can_read(MEMBER, wk))
        self.assertFalse(can_write(MEMBER, wk))


class ManageTest(unittest.TestCase):
    """删库与管成员比改资料更严：editor 只能维护内容。"""

    def test_editor_writes_content_but_cannot_manage(self):
        shared = coll(owner_id=MEMBER["id"])
        self.assertTrue(can_write(OTHER, shared, "editor"))
        self.assertFalse(can_manage(OTHER, shared))

    def test_admin_and_owner_manage_shared(self):
        shared = coll(owner_id=MEMBER["id"])
        self.assertTrue(can_manage(ADMIN, shared))
        self.assertTrue(can_manage(MEMBER, shared))
        self.assertFalse(can_manage(None, shared))

    def test_private_only_owner_manages(self):
        private = coll(visibility="private", owner_id=MEMBER["id"])
        self.assertTrue(can_manage(MEMBER, private))
        self.assertFalse(can_manage(ADMIN, private))
        self.assertFalse(can_manage(OTHER, private))

    def test_builtin_and_weknora(self):
        self.assertFalse(can_manage(ADMIN, {"id": "cs408", "builtin": True}))
        self.assertTrue(can_manage(ADMIN, coll(weknora=True)))
        self.assertFalse(can_manage(MEMBER, coll(weknora=True)))


class DescribeTest(unittest.TestCase):
    def test_adds_role_and_can_write_without_mutating(self):
        shared = coll(owner_id=MEMBER["id"])
        out = describe(MEMBER, shared)
        self.assertEqual(out["my_role"], "owner")
        self.assertTrue(out["can_write"])
        self.assertTrue(out["can_manage"])
        self.assertNotIn("my_role", shared)
        viewer = describe(OTHER, shared)
        self.assertEqual(viewer["my_role"], "viewer")
        self.assertFalse(viewer["can_manage"])
        self.assertEqual(describe(ADMIN, shared)["my_role"], "admin")

    def test_is_admin(self):
        self.assertTrue(is_admin(ADMIN))
        self.assertFalse(is_admin(MEMBER))
        self.assertFalse(is_admin(None))


if __name__ == "__main__":
    unittest.main()
