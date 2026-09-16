"""应用身份适配层：读取配置并委托给纯 domain 身份规则。"""
from __future__ import annotations

from app.config import settings
from app.domain.identity import (
    group_scope as _group_scope_for,
    is_owner_user as _is_owner_user_for,
    normalize_user_id as _normalize_user_id_for,
    resolve_owner_user_id,
    user_scope as _user_scope_for,
)


def owner_user_id() -> str:
    """返回当前配置的主人内部 ID。"""
    return resolve_owner_user_id(settings.qq_admin_id)


def normalize_user_id(user_id: str | None) -> str:
    """使用当前配置规范化外部用户 ID。"""
    return _normalize_user_id_for(user_id, owner_id=owner_user_id())


def is_owner_user(user_id: str) -> bool:
    """判断用户是否为当前配置的主人。"""
    return _is_owner_user_for(user_id, owner_id=owner_user_id())


def user_scope(uid: str, col: str = "user_id") -> tuple[str, tuple]:
    """生成当前主人配置下的用户 SQL scope。"""
    return _user_scope_for(uid, owner_id=owner_user_id(), col=col)


def group_scope(group_id: str | None, prefix: str = "") -> tuple[str, tuple]:
    """生成群 SQL scope。"""
    return _group_scope_for(group_id, prefix=prefix)


# 旧模块/调用方仍使用私有名称；迁移完成前保留别名。
_user_scope = user_scope
_group_scope = group_scope
