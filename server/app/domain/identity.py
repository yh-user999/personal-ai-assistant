"""纯用户身份与作用域规则。

本模块不读取配置、不连接数据库；应用层通过 ``owner_id`` 注入主人身份。
"""
from __future__ import annotations

OWNER_SENTINEL = "owner"


def resolve_owner_user_id(configured_id: str | None) -> str:
    """把配置中的主人 ID 规范化；未配置时使用本地/测试哨兵。"""
    return str(configured_id or "").strip() or OWNER_SENTINEL


def normalize_user_id(user_id: str | None, *, owner_id: str | None) -> str:
    """规范化外部用户 ID；空值代表主人，非法访客 ID fail-closed。"""
    owner = resolve_owner_user_id(owner_id)
    if user_id is None or not str(user_id).strip():
        return owner
    uid = str(user_id).strip()
    if uid == owner:
        return uid
    if not uid.isdigit() or len(uid) > 12:
        raise ValueError("非法 user_id：必须是 1-12 位数字 QQ 号")
    return uid


def is_owner_user(user_id: str, *, owner_id: str | None) -> bool:
    """判断内部用户 ID 是否为当前主人。"""
    return user_id == resolve_owner_user_id(owner_id)


def user_scope(uid: str, *, owner_id: str | None, col: str = "user_id") -> tuple[str, tuple]:
    """生成用户范围 SQL 子句；主人兼容旧数据中的空 user_id 行。"""
    if uid == resolve_owner_user_id(owner_id):
        return f"{col} IN (?, '')", (uid,)
    return f"{col} = ?", (uid,)


def group_scope(group_id: str | None, prefix: str = "") -> tuple[str, tuple]:
    """生成群范围 SQL 子句；空值代表私聊作用域。"""
    col = f"{prefix}group_id" if prefix else "group_id"
    return f"{col} = ?", (str(group_id or "").strip(),)
