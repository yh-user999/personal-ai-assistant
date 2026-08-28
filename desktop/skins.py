"""机器人皮肤系统：班德金属风（硬核机械版）/ 白色宇航员风（重制版）/ 原版萌系风。

皮肤选择持久化到 QSettings（跨重启记住），悬浮机器人/聊天头像/托盘图标
三处载体共用同一套皮肤。
视觉预览：python scripts/render_robot_preview.py（产物在 scripts/preview_out/）。
"""
from PySide6.QtCore import QSettings

ORG, APP = "PersonalAI", "Assistant"
_SKIN_KEY = "robot_skin"

SKIN_NAMES = {
    "bender": "班德金属风",
    "astro": "白色宇航员风",
    "classic": "原版萌系风",
}


def current_skin() -> str:
    """当前皮肤名（默认班德风，向后兼容）。"""
    name = QSettings(ORG, APP).value(_SKIN_KEY, "bender")
    return name if name in SKIN_NAMES else "bender"


def set_skin(name: str) -> None:
    if name in SKIN_NAMES:
        QSettings(ORG, APP).setValue(_SKIN_KEY, name)
