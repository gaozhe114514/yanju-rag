# -*- coding: utf-8 -*-
"""账号与个人数据同步的单元测试：不需要 PostgreSQL。"""
import unittest

from api.accounts import (AccountError, AccountService, PASSWORD_ITERATIONS,
                          SESSION_TTL_SECONDS, hash_password, verify_password)


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_service(**kw):
    clock = kw.pop("clock", None) or FakeClock()
    return AccountService(clock=clock), clock


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("正确密码123")
        self.assertTrue(stored.startswith("pbkdf2_sha256$%d$" % PASSWORD_ITERATIONS))
        self.assertTrue(verify_password("正确密码123", stored))
        self.assertFalse(verify_password("错误密码123", stored))

    def test_salt_is_random(self):
        a = hash_password("same-password")
        b = hash_password("same-password")
        self.assertNotEqual(a, b)

    def test_garbage_hash_is_false(self):
        self.assertFalse(verify_password("x", ""))
        self.assertFalse(verify_password("x", "plain-text"))
        self.assertFalse(verify_password("x", "md5$1$aa$bb"))


class RegisterTest(unittest.TestCase):
    def test_register_and_public_shape(self):
        svc, _ = make_service()
        out = svc.register("小明_408", "password123")
        self.assertTrue(out["token"])
        self.assertEqual(out["user"]["username"], "小明_408")
        # 内存模式的创建时间是时间戳，返回前要转成 ISO，不能把浮点数直接吐出去
        self.assertTrue(out["user"]["created_at"].startswith("20"))
        self.assertNotIn("password", str(out["user"]))

    def test_username_rules(self):
        svc, _ = make_service()
        for bad in ("a", " ", "有 空格", "x" * 25, "semi;colon"):
            with self.assertRaises(AccountError) as ctx:
                svc.register(bad, "password123")
            self.assertEqual(ctx.exception.status, 400)

    def test_password_rules(self):
        svc, _ = make_service()
        for bad in ("short", "        ", "x" * 129):
            with self.assertRaises(AccountError) as ctx:
                svc.register("normal-user", bad)
            self.assertEqual(ctx.exception.status, 400)

    def test_duplicate_is_case_insensitive(self):
        svc, _ = make_service()
        svc.register("Alice", "password123")
        with self.assertRaises(AccountError) as ctx:
            svc.register("alice", "password456")
        self.assertEqual(ctx.exception.status, 409)


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.svc.register("Alice", "password123")

    def test_login_and_authenticate(self):
        out = self.svc.login("alice", "password123", ip="1.1.1.1")
        user = self.svc.authenticate(out["token"])
        self.assertEqual(user["username"], "Alice")

    def test_wrong_password(self):
        with self.assertRaises(AccountError) as ctx:
            self.svc.login("alice", "wrong-password", ip="1.1.1.1")
        self.assertEqual(ctx.exception.status, 401)

    def test_unknown_user_is_same_error(self):
        with self.assertRaises(AccountError) as ctx:
            self.svc.login("nobody", "password123", ip="1.1.1.1")
        self.assertEqual(ctx.exception.status, 401)

    def test_fail_rate_limit_then_success_still_blocked_until_window(self):
        for _ in range(5):
            with self.assertRaises(AccountError):
                self.svc.login("alice", "bad-password", ip="2.2.2.2")
        with self.assertRaises(AccountError) as ctx:
            self.svc.login("alice", "password123", ip="2.2.2.2")
        self.assertEqual(ctx.exception.status, 429)
        # 过了窗口就恢复
        self.clock.now += 601
        self.assertTrue(self.svc.login("alice", "password123", ip="2.2.2.2")["token"])

    def test_success_clears_failures(self):
        with self.assertRaises(AccountError):
            self.svc.login("alice", "bad-password", ip="3.3.3.3")
        self.svc.login("alice", "password123", ip="3.3.3.3")
        for _ in range(4):
            with self.assertRaises(AccountError):
                self.svc.login("alice", "bad-password", ip="3.3.3.3")
        # 成功登录已把计数清零，5 次失败里还剩最后一次机会
        self.assertTrue(self.svc.login("alice", "password123", ip="3.3.3.3")["token"])

    def test_logout_invalidates_session(self):
        out = self.svc.login("alice", "password123", ip="4.4.4.4")
        self.svc.logout(out["token"])
        self.assertIsNone(self.svc.authenticate(out["token"]))

    def test_session_expires(self):
        out = self.svc.login("alice", "password123", ip="5.5.5.5")
        self.clock.now += SESSION_TTL_SECONDS - 1
        self.assertIsNotNone(self.svc.authenticate(out["token"]))
        self.clock.now += 2
        self.assertIsNone(self.svc.authenticate(out["token"]))

    def test_unknown_token(self):
        self.assertIsNone(self.svc.authenticate("not-a-real-token"))
        self.assertIsNone(self.svc.authenticate(""))


class PasswordChangeTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        out = self.svc.register("Alice", "password123")
        self.uid = out["user"]["id"]
        self.first = out["token"]

    def test_change_password_keeps_current_session(self):
        res = self.svc.change_password(
            self.uid, "password123", "new-password-456",
            ip="9.9.9.9", keep_token=self.first)
        self.assertTrue(res["ok"])
        self.assertEqual(res["sessions_revoked"], 0)
        self.assertIsNotNone(self.svc.authenticate(self.first))
        with self.assertRaises(AccountError) as ctx:
            self.svc.login("alice", "password123", ip="9.9.9.9")
        self.assertEqual(ctx.exception.status, 401)
        self.assertTrue(self.svc.login("alice", "new-password-456", ip="9.9.9.9")["token"])

    def test_other_sessions_are_revoked(self):
        second = self.svc.login("alice", "password123", ip="9.9.9.8")
        res = self.svc.change_password(
            self.uid, "password123", "new-password-456", keep_token=self.first)
        self.assertEqual(res["sessions_revoked"], 1)
        self.assertIsNotNone(self.svc.authenticate(self.first))
        self.assertIsNone(self.svc.authenticate(second["token"]))

    def test_wrong_old_password_is_rate_limited(self):
        with self.assertRaises(AccountError) as ctx:
            self.svc.change_password(self.uid, "bad-password", "new-password-456",
                                     ip="9.9.9.7")
        self.assertEqual(ctx.exception.status, 401)
        for _ in range(4):
            with self.assertRaises(AccountError):
                self.svc.change_password(self.uid, "bad-password", "new-password-456",
                                         ip="9.9.9.7")
        with self.assertRaises(AccountError) as ctx:
            self.svc.change_password(self.uid, "password123", "new-password-456",
                                     ip="9.9.9.7")
        self.assertEqual(ctx.exception.status, 429)

    def test_new_password_rules(self):
        with self.assertRaises(AccountError) as ctx:
            self.svc.change_password(self.uid, "password123", "short")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(AccountError) as ctx:
            self.svc.change_password(self.uid, "password123", "password123")
        self.assertEqual(ctx.exception.status, 400)

    def test_unknown_user(self):
        with self.assertRaises(AccountError) as ctx:
            self.svc.change_password("u-nope", "password123", "new-password-456")
        self.assertEqual(ctx.exception.status, 404)


class RoleTest(unittest.TestCase):
    def test_first_user_is_admin_second_is_member(self):
        svc, _ = make_service()
        first = svc.register("Alice", "password123")
        second = svc.register("Bob", "password123")
        self.assertEqual(first["user"]["role"], "admin")
        self.assertEqual(second["user"]["role"], "member")
        # 公开信息里不能出现口令哈希
        self.assertNotIn("password_hash", first["user"])

    def test_admin_names_are_case_insensitive(self):
        svc = AccountService(admin_names=["Boss"])
        svc.register("Alice", "password123")   # 第一个兜底管理员
        out = svc.register("boss", "password123")
        self.assertEqual(out["user"]["role"], "admin")

    def test_find_and_get_user(self):
        svc, _ = make_service()
        out = svc.register("Alice", "password123")
        found = svc.find_user("alice")
        self.assertEqual(found["id"], out["user"]["id"])
        self.assertEqual(found["role"], "admin")
        self.assertEqual(svc.get_user(found["id"])["username"], "Alice")
        self.assertIsNone(svc.find_user("nobody"))
        self.assertIsNone(svc.get_user("u-nope"))

    def test_ensure_admin_promotes_configured_user(self):
        svc, _ = make_service()
        svc.register("Alice", "password123")
        svc.register("Bob", "password123")
        self.assertEqual(svc.find_user("bob")["role"], "member")
        svc.admin_names = frozenset({"bob"})
        self.assertEqual(svc.ensure_admin(), ["Bob"])
        self.assertEqual(svc.find_user("bob")["role"], "admin")


class DataSyncTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.a = self.svc.register("alice", "password123")
        self.b = self.svc.register("bob", "password123")

    def put(self, user, key, value, ts):
        return self.svc.put_data(user["user"]["id"], {
            key: {"value": value, "updated_at": ts}})

    def test_round_trip(self):
        out = self.put(self.a, "408-conversations-v1", '{"x":1}', 5000)
        self.assertEqual(out["applied"], ["408-conversations-v1"])
        data = self.svc.get_data(self.a["user"]["id"])
        self.assertEqual(data["408-conversations-v1"],
                         {"value": '{"x":1}', "updated_at": 5000})

    def test_newer_wins_and_older_is_rejected(self):
        self.put(self.a, "408-notebooks-v1", "new", 5000)
        out = self.put(self.a, "408-notebooks-v1", "old", 4000)
        self.assertEqual(out["applied"], [])
        self.assertEqual(out["server"]["408-notebooks-v1"], 5000)
        self.assertEqual(
            self.svc.get_data(self.a["user"]["id"])["408-notebooks-v1"]["value"],
            "new")
        out = self.put(self.a, "408-notebooks-v1", "newer", 6000)
        self.assertEqual(out["applied"], ["408-notebooks-v1"])

    def test_reserved_keys_are_refused(self):
        for key in ("408-access-token", "408-account-token-v1",
                    "408-sync-meta-v1"):
            with self.assertRaises(AccountError) as ctx:
                self.put(self.a, key, "secret", 5000)
            self.assertEqual(ctx.exception.status, 400)

    def test_bad_key_and_value(self):
        with self.assertRaises(AccountError):
            self.put(self.a, "conversations", "{}", 5000)
        with self.assertRaises(AccountError):
            self.svc.put_data(self.a["user"]["id"], {
                "408-x-v1": {"value": {"not": "text"}, "updated_at": 1}})
        with self.assertRaises(AccountError) as ctx:
            self.put(self.a, "408-big-v1", "x" * (512 * 1024 + 1), 5000)
        self.assertEqual(ctx.exception.status, 413)

    def test_future_timestamp_is_clamped(self):
        crazy = int((self.clock.now + 10 * 86400) * 1000)
        self.put(self.a, "408-mastery-v1", "[]", crazy)
        stored = self.svc.get_data(self.a["user"]["id"])["408-mastery-v1"]
        self.assertLessEqual(stored["updated_at"],
                             int(self.clock.now * 1000) + 86400 * 1000)

    def test_users_are_isolated(self):
        self.put(self.a, "408-journal-v1", "alice-data", 5000)
        self.assertEqual(self.svc.get_data(self.b["user"]["id"]), {})
        self.svc.delete_data(self.a["user"]["id"], delete_all=True)
        left = self.svc.get_data(self.a["user"]["id"])
        self.assertEqual(left["408-journal-v1"]["value"], "__408_deleted__")

    def test_delete_selected_keys(self):
        self.put(self.a, "408-journal-v1", "1", 5000)
        self.put(self.a, "408-task-board-v1", "2", 5000)
        removed = self.svc.delete_data(
            self.a["user"]["id"], ["408-journal-v1"])
        self.assertEqual(removed, 1)
        left = self.svc.get_data(self.a["user"]["id"])
        self.assertEqual(left["408-journal-v1"]["value"], "__408_deleted__")
        self.assertEqual(left["408-task-board-v1"]["value"], "2")

    def test_deleted_key_can_be_written_again(self):
        self.put(self.a, "408-journal-v1", "1", 5000)
        self.clock.now += 10
        self.svc.delete_data(self.a["user"]["id"], ["408-journal-v1"])
        out = self.put(self.a, "408-journal-v1", "2",
                       int(self.clock.now * 1000) + 1000)
        self.assertEqual(out["applied"], ["408-journal-v1"])
        self.assertEqual(
            self.svc.get_data(self.a["user"]["id"])["408-journal-v1"]["value"], "2")

    def test_too_many_items(self):
        items = {"408-k%02d-v1" % i: {"value": "x", "updated_at": 1}
                 for i in range(81)}
        with self.assertRaises(AccountError) as ctx:
            self.svc.put_data(self.a["user"]["id"], items)
        self.assertEqual(ctx.exception.status, 400)


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.admin = self.svc.register("Boss", "password123")["user"]
        self.member = self.svc.register("Alice", "password123")
        self.uid = self.member["user"]["id"]

    def test_list_users_requires_admin(self):
        users = self.svc.list_users(self.admin)
        self.assertEqual([u["username"] for u in users], ["Boss", "Alice"])
        self.assertEqual([u["role"] for u in users], ["admin", "member"])
        self.assertFalse(users[1]["disabled"])
        with self.assertRaises(AccountError) as ctx:
            self.svc.list_users(self.member["user"])
        self.assertEqual(ctx.exception.status, 403)

    def test_promote_and_demote(self):
        out = self.svc.update_user(self.admin, self.uid, role="admin")
        self.assertEqual(out["role"], "admin")
        self.assertEqual(self.svc.authenticate(self.member["token"])["role"], "admin")
        out = self.svc.update_user(self.admin, self.uid, role="member")
        self.assertEqual(out["role"], "member")

    def test_bad_role_and_unknown_user(self):
        with self.assertRaises(AccountError) as ctx:
            self.svc.update_user(self.admin, self.uid, role="owner")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(AccountError) as ctx:
            self.svc.update_user(self.admin, "u-nope", role="member")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(AccountError) as ctx:
            self.svc.update_user(self.admin, self.uid)
        self.assertEqual(ctx.exception.status, 400)

    def test_self_protection(self):
        me = self.admin["id"]
        for kw in ({"role": "member"}, {"disabled": True}):
            with self.assertRaises(AccountError) as ctx:
                self.svc.update_user(self.admin, me, **kw)
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.svc.get_user(me)["role"], "admin")
        self.assertFalse(self.svc.get_user(me)["disabled"])

    def test_disable_revokes_sessions_and_blocks_login(self):
        token = self.member["token"]
        self.assertIsNotNone(self.svc.authenticate(token))
        out = self.svc.update_user(self.admin, self.uid, disabled=True)
        self.assertTrue(out["disabled"])
        self.assertIsNone(self.svc.authenticate(token))
        with self.assertRaises(AccountError) as ctx:
            self.svc.login("alice", "password123", ip="6.6.6.6")
        self.assertEqual(ctx.exception.status, 403)
        # 启用后旧密码照常登录，数据没动
        out = self.svc.update_user(self.admin, self.uid, disabled=False)
        self.assertFalse(out["disabled"])
        self.assertTrue(self.svc.login("alice", "password123",
                                       ip="6.6.6.6")["token"])

    def test_disabled_admin_loses_admin_power(self):
        self.svc.update_user(self.admin, self.uid, role="admin")
        token = self.svc.login("alice", "password123", ip="7.7.7.7")["token"]
        alice = self.svc.authenticate(token)
        self.assertEqual(alice["role"], "admin")
        self.svc.update_user(self.admin, self.uid, disabled=True)
        # 停用会踢掉已有会话，再登录也进不来
        self.assertIsNone(self.svc.authenticate(token))
        with self.assertRaises(AccountError) as login_ctx:
            self.svc.login("alice", "password123", ip="7.7.7.8")
        self.assertEqual(login_ctx.exception.status, 403)
        # 停用前拿到的身份也不好使：服务端每次都回库查
        with self.assertRaises(AccountError) as ctx:
            self.svc.list_users(alice)
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
