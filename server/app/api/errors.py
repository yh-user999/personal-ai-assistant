"""API 错误响应统一形状：detail 恒为 ``{code, message}``。

novel API 早已是该形状；其余端点历史上是裸字符串 detail，导致前端
apiFetch 只能显示"请求失败（HTTP xxx）"，后端精心写的中文文案到不了用户。
新端点一律用本助手构造错误；存量端点逐步迁移。
"""
from __future__ import annotations

from fastapi import HTTPException


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    """结构化 API 错误。``code`` 用稳定英文标识（便于程序判断），
    ``message`` 是可直接展示给用户的中文文案。"""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
