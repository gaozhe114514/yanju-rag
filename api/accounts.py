# -*- coding: utf-8 -*-
"""账号：注册、登录、会话与个人数据同步。

只做三件事：

1. 用户名 + 口令注册，口令用 PBKDF2-HMAC-SHA256（每用户独立盐）存哈希，
   明文不落盘、不写日志；
2. 登录发一枚随机 token，数据库里只存它的 SHA-256，会话 30 天过期；
3. 登录后把浏览器里 `408-*` 的个人数据（对话、笔记本、题库、精通之路……）
   按「最后写入时间」合并到账号，换设备也能接着用。

没有 DATABASE_URL 时退到进程内存，方便本地开发和单测；进程重启后账号会丢，
README 里写明了这一点，不假装持久化。
"""
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from db.store import DuplicateUser

PASSWORD_ITERATIONS = 210_000
SESSION_TTL_SECONDS = 30 * 24 * 3600
# v0.67：每次使用把过期时间往后顺延，但一天内不重复写库，
# 否则每个带 token 的请求都会写一次 user_sessions。
SESSION_RENEW_AFTER = 24 * 3600
MAX_SYNC_ITEMS = 80
MAX_SYNC_VALUE = 512 * 1024
LOGIN_FAIL_MAX = 5
LOGIN_FAIL_WINDOW = 600

USERNAME_RE = re.compile(r"^[\w一-鿿.-]+$", re.UNICODE)
SYNC_KEY_RE = re.compile(r"^408-[A-Za-z0-9._-]{1,60}$")

# 口令和会话 token 绝不能同步到服务器；前端也有一份同样的黑名单。
RESERVED_SYNC_KEYS = frozenset({
    "408-access-token",
    "408-account-token-v1",
    "408-sync-meta-v1",
})

# 删除也要跨设备生效：不物理删行，而是把值改成这个标记并更新时间。
# 别的设备下次同步看到更新的墓碑，就把本地那份删掉。
DELETED_MARK = "__408_deleted__"


class AccountError(Exception):
    """带 HTTP 状态码的业务错误，路由层直接转成 detail。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = message


def clean_username(raw) -> str:
    name = str(raw or "").strip()
    if len(name) < 2 or len(name) > 24:
        raise AccountError(400, "用户名要 2–24 个字符")
    if not USERNAME_RE.match(name):
        raise AccountError(400, "用户名只能用中文、字母、数字、下划线、点或短横线")
    return name


def clean_password(raw) -> str:
    password = str(raw or "")
    if len(password) < 8:
        raise AccountError(400, "密码至少 8 位")
    if len(password) > 128:
        raise AccountError(400, "密码最多 128 位")
    if not password.strip():
        raise AccountError(400, "密码不能全是空格")
    return password


def username_key(name: str) -> str:
    """唯一键：英文大小写不敏感；中文不受影响。"""
    return str(name or "").strip().casefold()


def hash_password(password: str, iterations: int = PASSWORD_ITERATIONS,
                  salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations))
    return "pbkdf2_sha256$%d$%s$%s" % (
        int(iterations), salt.hex(), digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, want_hex = str(stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), want_hex)


_DUMMY_HASH = ""


def dummy_hash() -> str:
    """用户名不存在时也做一次同样开销的校验，避免用响应时间猜账号是否存在。"""
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password("__not_a_real_password__")
    return _DUMMY_HASH


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError):
        return str(value)


def _ms(value) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(float(value) * 1000)


class AccountService:
    """账号服务。store 为 None 时用进程内存（重启即丢）。

    admin_names 来自环境变量 KB_ADMIN_USERS，逗号分隔；没有环境变量时
    第一个注册的账号自动成为管理员。
    """

    def __init__(self, store=None, clock=None, admin_names=None):
        self.store = store
        self.clock = clock or time.time
        self.admin_names = frozenset(
            username_key(n) for n in (admin_names or []) if str(n or "").strip())
        self._lock = threading.Lock()
        self._users: Dict[str, Dict] = {}
        self._by_key: Dict[str, str] = {}
        self._sessions: Dict[str, Dict] = {}
        self._data: Dict[Tuple[str, str], Dict] = {}
        self._fails: Dict[Tuple[str, str], deque] = {}

    # ---------- 注册 / 登录 ----------

    def register(self, username, password, ip: str = "") -> Dict:
        name = clean_username(username)
        password = clean_password(password)
        key = username_key(name)
        user_id = "u-" + secrets.token_hex(8)
        pw_hash = hash_password(password)
        now = self._now()

        if self.store is not None:
            # KB_ADMIN_USERS 指定的人直接是管理员；否则第一个注册的人兜底。
            role = "admin" if (key in self.admin_names
                               or not self.store.has_admin()) else "member"
            try:
                row = self.store.create_user(user_id, name, key, pw_hash,
                                             role=role)
            except DuplicateUser:
                raise AccountError(409, "这个用户名已经被注册了")
        else:
            with self._lock:
                if key in self._by_key:
                    raise AccountError(409, "这个用户名已经被注册了")
                has_admin = any(u.get("role") == "admin"
                                and not u.get("disabled")
                                for u in self._users.values())
                role = "admin" if (key in self.admin_names or not has_admin) \
                    else "member"
                row = {
                    "id": user_id,
                    "username": name,
                    "role": role,
                    "disabled": False,
                    "created_at": now,
                    "last_login_at": now,
                }
                self._users[user_id] = dict(row, username_key=key,
                                            password_hash=pw_hash)
                self._by_key[key] = user_id

        return self._issue(row)

    def login(self, username, password, ip: str = "") -> Dict:
        name = str(username or "").strip()
        key = username_key(name)
        self._check_fail_rate(ip, key)
        user = (self.store.get_user_by_key(key) if self.store is not None
                else self._memory_user_by_key(key))
        stored = user["password_hash"] if user else dummy_hash()
        if not verify_password(str(password or ""), stored) or user is None:
            self._record_fail(ip, key)
            raise AccountError(401, "用户名或密码不对")
        if user.get("disabled"):
            raise AccountError(403, "这个账号已被停用，请联系管理员")

        if self.store is not None:
            self._store_clear_fails(ip, key)
            user = self.store.touch_login(user["id"]) or user
            if key in self.admin_names and user.get("role") != "admin":
                self.store.set_user_role(user["id"], "admin")
                user = dict(user, role="admin")
        else:
            with self._lock:
                user = dict(user)
                user["last_login_at"] = self._now()
                if key in self.admin_names:
                    user["role"] = "admin"
                self._users[user["id"]] = dict(
                    user, password_hash=self._users[user["id"]]["password_hash"],
                    username_key=key)
                self._fails.pop((ip, key), None)
        return self._issue(user)

    def authenticate(self, token: str) -> Optional[Dict]:
        raw = str(token or "").strip()
        if not raw:
            return None
        hashed = token_hash(raw)
        now = self._now()
        if self.store is not None:
            row = self.store.get_session(hashed)
            if not row:
                return None
            # 滑动过期：用过就往后顺延 30 天，但一天内只写一次库
            if self._should_renew(row.get("expires_at"), now):
                self.store.renew_session(
                    hashed, datetime.fromtimestamp(
                        now + SESSION_TTL_SECONDS, tz=timezone.utc))
            return self._public(row)
        with self._lock:
            session = self._sessions.get(hashed)
            if session is None:
                return None
            if session["expires_at"] <= now:
                self._sessions.pop(hashed, None)
                return None
            user = self._users.get(session["user_id"])
            if not user or user.get("disabled"):
                return None
            if self._should_renew(session["expires_at"], now):
                session["expires_at"] = now + SESSION_TTL_SECONDS
            return self._public(user)

    def logout(self, token: str) -> None:
        raw = str(token or "").strip()
        if not raw:
            return
        hashed = token_hash(raw)
        if self.store is not None:
            self.store.delete_session(hashed)
            return
        with self._lock:
            self._sessions.pop(hashed, None)

    def change_password(self, user_id: str, old_password, new_password,
                        ip: str = "", keep_token: str = "") -> Dict:
        """改密码：验原密码，换新哈希，其它设备的会话全部失效。

        ``keep_token`` 是发起这次修改的那台设备，保留它，免得刚改完就被登出。
        原密码错也走登录失败计数，不给穷举留口子。
        """
        raw = self._raw_user(user_id)
        if raw is None:
            raise AccountError(404, "账号不存在")
        key = username_key(raw.get("username"))
        self._check_fail_rate(ip, key)
        if not verify_password(str(old_password or ""),
                               raw.get("password_hash") or dummy_hash()):
            self._record_fail(ip, key)
            raise AccountError(401, "原密码不对")
        password = clean_password(new_password)
        if verify_password(password, raw.get("password_hash") or ""):
            raise AccountError(400, "新密码不能和原密码一样")
        self._store_clear_fails(ip, key)
        pw_hash = hash_password(password)
        keep = token_hash(keep_token) if keep_token else ""
        if self.store is not None:
            self.store.update_password(user_id, pw_hash)
            revoked = self.store.delete_user_sessions(user_id, keep)
        else:
            with self._lock:
                self._users[user_id]["password_hash"] = pw_hash
                revoked = 0
                for hashed, session in list(self._sessions.items()):
                    if session["user_id"] == user_id and hashed != keep:
                        self._sessions.pop(hashed, None)
                        revoked += 1
        return {"ok": True, "sessions_revoked": int(revoked)}

    # ---------- 管理员：账号管理 ----------

    def _require_admin(self, actor: Dict) -> Dict:
        """确认调用者还是可用的管理员；停用的账号立刻失去管理权限。"""
        if not actor or actor.get("role") != "admin":
            raise AccountError(403, "只有管理员能管理账号")
        fresh = self.get_user(str(actor.get("id") or ""))
        if fresh is None or fresh.get("disabled"):
            raise AccountError(403, "只有管理员能管理账号")
        return fresh

    def list_users(self, actor: Dict) -> List[Dict]:
        self._require_admin(actor)
        if self.store is not None:
            rows = self.store.list_users()
        else:
            # 内存模式按注册顺序返回（dict 保持插入序）；同一秒注册的账号
            # 用 id 排会乱，这里不排。
            with self._lock:
                rows = [dict(u) for u in self._users.values()]
        return [self._public(r) for r in rows]

    def _active_admin_count(self, exclude_id: str = "") -> int:
        if self.store is not None:
            return self.store.count_admins(exclude_id)
        with self._lock:
            return sum(1 for u in self._users.values()
                       if u.get("role") == "admin" and not u.get("disabled")
                       and u.get("id") != exclude_id)

    def update_user(self, actor: Dict, user_id: str, role: Optional[str] = None,
                    disabled: Optional[bool] = None) -> Dict:
        """改角色或停用/启用账号。

        两条红线：不能动自己（免得把自己锁在门外），也不能把最后一个可用
        管理员降级或停用。停用不删数据，会话立即失效，重新启用就回来。
        """
        me = self._require_admin(actor)
        target = self.get_user(user_id)
        if target is None:
            raise AccountError(404, "账号不存在")
        if role is None and disabled is None:
            raise AccountError(400, "没有要改的东西")
        is_self = target["id"] == me["id"]
        if is_self and (role is not None or disabled):
            raise AccountError(400, "不能修改自己的角色或停用自己")

        if role is not None:
            role = str(role or "").strip()
            if role not in ("admin", "member"):
                raise AccountError(400, "角色只能是 admin 或 member")
            if role != "admin" and target["role"] == "admin" \
                    and self._active_admin_count(exclude_id=target["id"]) == 0:
                raise AccountError(400, "至少保留一个可用的管理员")

        if self.store is not None:
            if role is not None:
                self.store.set_user_role(target["id"], role)
            if disabled is not None:
                self.store.set_user_disabled(target["id"], bool(disabled))
                if disabled:
                    self.store.delete_user_sessions(target["id"])
        else:
            with self._lock:
                row = self._users[target["id"]]
                if role is not None:
                    row["role"] = role
                if disabled is not None:
                    row["disabled"] = bool(disabled)
                    if disabled:
                        for hashed, session in list(self._sessions.items()):
                            if session["user_id"] == target["id"]:
                                self._sessions.pop(hashed, None)
        return self.get_user(target["id"]) or target

    def find_user(self, username: str) -> Optional[Dict]:
        key = username_key(username)
        if not key:
            return None
        row = (self.store.get_user_by_key(key) if self.store is not None
               else self._memory_user_by_key(key))
        return self._public(row) if row else None

    def get_user(self, user_id: str) -> Optional[Dict]:
        if not user_id:
            return None
        if self.store is not None:
            row = self.store.get_user_by_id(user_id)
        else:
            with self._lock:
                row = dict(self._users[user_id]) if user_id in self._users else None
        return self._public(row) if row else None

    def ensure_admin(self) -> List[str]:
        """启动时补管理员：一个都没有就把最早注册的提上来。"""
        promoted: List[str] = []
        if self.store is not None:
            if not self.store.has_admin():
                name = self.store.promote_first_admin()
                if name:
                    promoted.append(name)
            self.store.promote_admins(sorted(self.admin_names))
        else:
            with self._lock:
                if not any(u.get("role") == "admin" and not u.get("disabled")
                           for u in self._users.values()):
                    first = next((u for u in self._users.values()
                                  if not u.get("disabled")), None)
                    if first is not None:
                        first["role"] = "admin"
                        promoted.append(first["username"])
                for key in self.admin_names:
                    uid = self._by_key.get(key)
                    if uid and self._users[uid].get("role") != "admin":
                        self._users[uid]["role"] = "admin"
                        promoted.append(self._users[uid]["username"])
        return promoted

    def _issue(self, user: Dict) -> Dict:
        token = new_token()
        hashed = token_hash(token)
        expires_at = self._now() + SESSION_TTL_SECONDS
        if self.store is not None:
            self.store.delete_expired_sessions(user["id"])
            self.store.create_session(
                hashed, user["id"],
                datetime.fromtimestamp(expires_at, tz=timezone.utc))
        else:
            with self._lock:
                self._sessions[hashed] = {
                    "user_id": user["id"],
                    "expires_at": expires_at,
                }
        return {
            "user": self._public(user),
            "token": token,
            "expires_at": int(expires_at * 1000),
        }

    # ---------- 个人数据 ----------

    def get_data(self, user_id: str) -> Dict[str, Dict]:
        if self.store is not None:
            rows = self.store.get_user_data(user_id)
            return {k: {"value": v["value"], "updated_at": _ms(v["updated_at"])}
                    for k, v in rows.items()}
        with self._lock:
            return {
                key: {"value": rec["value"], "updated_at": int(rec["updated_at"] * 1000)}
                for (uid, key), rec in self._data.items() if uid == user_id
            }

    def put_data(self, user_id: str, items: Dict) -> Dict:
        """按时间戳合并；比服务器旧的不覆盖，返回被拒的键。"""
        clean = {}
        if len(items or {}) > MAX_SYNC_ITEMS:
            raise AccountError(400, "一次最多同步 %d 个键" % MAX_SYNC_ITEMS)
        now_ms = int(self._now() * 1000)
        ceiling = now_ms + 86400 * 1000
        for key, item in (items or {}).items():
            key = str(key or "")
            if not SYNC_KEY_RE.match(key):
                raise AccountError(400, "不能同步这个键：%s" % key[:40])
            if key in RESERVED_SYNC_KEYS:
                raise AccountError(400, "凭据类数据不允许同步")
            value = item.get("value") if isinstance(item, dict) else None
            if not isinstance(value, str):
                raise AccountError(400, "「%s」的值必须是一段文本" % key)
            if len(value) > MAX_SYNC_VALUE:
                raise AccountError(413, "「%s」超过 512KB，先清理一下" % key)
            updated = item.get("updated_at") if isinstance(item, dict) else 0
            try:
                updated = int(updated or 0)
            except (TypeError, ValueError):
                updated = 0
            if updated <= 0:
                updated = now_ms
            # 客户端时钟可能偏快；允许一天内的提前量，剩下的按服务器时间算，
            # 免得一个坏时间戳把后续同步永远挡在门外。
            if updated > ceiling:
                updated = now_ms
            clean[key] = (value, updated)

        if not clean:
            return {"applied": [], "server": {}}

        if self.store is not None:
            return self.store.put_user_data(user_id, clean)

        applied: List[str] = []
        server: Dict[str, int] = {}
        with self._lock:
            for key, (value, updated) in clean.items():
                rec = self._data.get((user_id, key))
                if rec is not None and rec["updated_at"] * 1000 > updated:
                    server[key] = int(rec["updated_at"] * 1000)
                    continue
                self._data[(user_id, key)] = {
                    "value": value,
                    "updated_at": updated / 1000.0,
                }
                applied.append(key)
        return {"applied": applied, "server": server}

    def delete_data(self, user_id: str, keys=None, delete_all: bool = False) -> int:
        if delete_all:
            keys = None
        if self.store is not None:
            return self.store.delete_user_data(user_id, keys)
        now = self._now()
        with self._lock:
            targets = [k for k in self._data if k[0] == user_id
                       and (delete_all or k[1] in set(keys or []))]
            for k in targets:
                self._data[k] = {"value": DELETED_MARK, "updated_at": now}
            return len(targets)

    # ---------- 内部 ----------

    def _now(self) -> float:
        return float(self.clock())

    def _should_renew(self, expires_at, now: float) -> bool:
        """剩余寿命不足「30 天 − 1 天」才续期，正常请求不写库。"""
        if expires_at is None:
            return False
        try:
            exp = (expires_at.timestamp() if isinstance(expires_at, datetime)
                   else float(expires_at))
        except (TypeError, ValueError, OSError):
            return False
        return exp < now + SESSION_TTL_SECONDS - SESSION_RENEW_AFTER

    def _public(self, user: Dict) -> Dict:
        return {
            "id": user.get("id", ""),
            "username": user.get("username", ""),
            "role": user.get("role") or "member",
            "disabled": bool(user.get("disabled")),
            "created_at": _iso(user.get("created_at")),
            "last_login_at": _iso(user.get("last_login_at")) or None,
        }

    def _raw_user(self, user_id: str) -> Optional[Dict]:
        """带口令哈希的内部记录，只给改密码用，不出路由层。"""
        if self.store is not None:
            return self.store.get_user_by_id(user_id)
        with self._lock:
            return dict(self._users[user_id]) if user_id in self._users else None

    def _memory_user_by_key(self, key: str) -> Optional[Dict]:
        with self._lock:
            uid = self._by_key.get(key)
            return dict(self._users[uid]) if uid else None

    def _check_fail_rate(self, ip: str, key: str) -> None:
        bucket = (ip or "-", key or "-")
        now = self._now()
        with self._lock:
            hits = self._fails.get(bucket)
            if not hits:
                return
            while hits and now - hits[0] > LOGIN_FAIL_WINDOW:
                hits.popleft()
            if len(hits) >= LOGIN_FAIL_MAX:
                raise AccountError(429, "密码错太多次了，等 10 分钟再试")

    def _record_fail(self, ip: str, key: str) -> None:
        bucket = (ip or "-", key or "-")
        with self._lock:
            hits = self._fails.setdefault(bucket, deque())
            hits.append(self._now())
            while hits and self._now() - hits[0] > LOGIN_FAIL_WINDOW:
                hits.popleft()

    def _store_clear_fails(self, ip: str, key: str) -> None:
        with self._lock:
            self._fails.pop((ip or "-", key or "-"), None)
