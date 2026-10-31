# -*- coding: utf-8 -*-
"""知识库权限（B-lite）。

规则只有三条，够公司场景用，也不至于把权限系统做成迷宫：

1. 内置 408 语料谁都能读，谁都不能改；
2. 公司库（shared）登录后可读，管理员或被指定的 owner / editor 可写；
3. 个人库（private）只有建库的人自己能读能写，管理员也不行。

这里是纯函数，不碰数据库；调用方负责把 collections.owner_id / visibility
和 collection_members 里的角色传进来。
"""

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"

VISIBILITY_BUILTIN = "builtin"
VISIBILITY_SHARED = "shared"
VISIBILITY_PRIVATE = "private"
VISIBILITIES = (VISIBILITY_SHARED, VISIBILITY_PRIVATE)

MEMBER_ROLE_OWNER = "owner"
MEMBER_ROLE_EDITOR = "editor"
MEMBER_ROLE_VIEWER = "viewer"
ASSIGNABLE_ROLES = (MEMBER_ROLE_EDITOR, MEMBER_ROLE_VIEWER)
WRITE_MEMBER_ROLES = (MEMBER_ROLE_OWNER, MEMBER_ROLE_EDITOR)


class AccessError(Exception):
    """带 HTTP 状态码的权限错误；路由层直接转成 detail。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = message


def is_admin(user) -> bool:
    return bool(user and user.get("role") == ADMIN_ROLE)


def is_owner(user, coll) -> bool:
    return bool(user and coll and coll.get("owner_id") == user.get("id"))


def visibility_of(coll) -> str:
    if not coll:
        return VISIBILITY_SHARED
    if coll.get("builtin"):
        return VISIBILITY_BUILTIN
    value = str(coll.get("visibility") or VISIBILITY_SHARED)
    return value if value in VISIBILITIES else VISIBILITY_SHARED


def can_read(user, coll, member_role: str = "") -> bool:
    """能不能看这个库（以及用它提问、看资料与片段）。"""
    if not coll:
        return False
    if coll.get("builtin"):
        return True
    # WeKnora 后端不归我们管，只有管理员能碰，避免绕过权限闸门。
    if coll.get("weknora"):
        return is_admin(user)
    if not user:
        return False
    if is_owner(user, coll):
        return True
    visibility = visibility_of(coll)
    if visibility == VISIBILITY_PRIVATE:
        return False
    return True  # 公司库：登录即可读


def can_write(user, coll, member_role: str = "") -> bool:
    """能不能增删资料、改片段。"""
    if not coll or coll.get("builtin"):
        return False
    if coll.get("weknora"):
        return is_admin(user)
    if not user:
        return False
    if is_owner(user, coll):
        return True
    # 个人库先挡住管理员：管理员也不能改别人的个人库。
    if visibility_of(coll) == VISIBILITY_PRIVATE:
        return False
    if is_admin(user):
        return True
    return member_role in WRITE_MEMBER_ROLES


def can_manage(user, coll) -> bool:
    """能不能删库、管成员。

    比 can_write 更严：editor 只维护资料，不能把整个库删掉，也不能给自己
    或别人发编辑权限。删库与增删成员只留给管理员和建库人。
    """
    if not coll or coll.get("builtin"):
        return False
    if coll.get("weknora"):
        return is_admin(user)
    if not user:
        return False
    if is_owner(user, coll):
        return True
    if visibility_of(coll) == VISIBILITY_PRIVATE:
        return False
    return is_admin(user)


def role_label(user, coll, member_role: str = "") -> str:
    """给前端显示「我在这个库里的身份」，不是权限判据。"""
    if not coll:
        return ""
    if coll.get("builtin"):
        return VISIBILITY_BUILTIN
    if is_admin(user):
        return ADMIN_ROLE
    if is_owner(user, coll):
        return MEMBER_ROLE_OWNER
    if member_role:
        return member_role
    if visibility_of(coll) == VISIBILITY_SHARED:
        return MEMBER_ROLE_VIEWER
    return ""


def describe(user, coll, member_role: str = "") -> dict:
    """把库对象补上前端要用的权限字段；不改原对象。"""
    out = dict(coll or {})
    out["visibility"] = visibility_of(coll)
    out["my_role"] = role_label(user, coll, member_role)
    out["can_write"] = can_write(user, coll, member_role)
    out["can_manage"] = can_manage(user, coll)
    return out
