"""面板/悬浮球主题系统：4 套配色，与机器人皮肤（形象）正交组合。

主题只管"颜色"（面板背景/气泡/文字/按钮/状态灯/LED），皮肤只管"造型"，
3 皮肤 × 4 主题自由组合。选择持久化 QSettings，右键菜单切换，立即生效。

对比度：各主题正文色对背景 ≥ WCAG AA（4.5:1），不做刺眼亮色主题。
"""
from PySide6.QtCore import QSettings

ORG, APP = "PersonalAI", "Assistant"
_THEME_KEY = "ui_theme"

THEME_NAMES = {
    "nightblue": "星夜蓝（默认）",
    "forest": "森屿绿",
    "twilight": "暮山紫",
    "obsidian": "墨玉金",
}

# token 语义：
#   card        面板卡片背景        input_bg   输入框/内嵌屏背景
#   bubble_me   用户气泡           bubble_ai   助手气泡
#   text_main   正文文字           text_sub    次要文字（时间戳/问候）
#   text_faint  更弱文字（占位）    border      通用描边
#   hover       悬停面板色          pressed    按压面板色
#   btn_bg      快捷按钮背景        scrollbar  滚动条手柄
#   accent      主强调色（发送键/输入框聚焦/LED）
#   accent_hover/pressed            强调色悬停/按压
#   state       状态灯 4 色（idle/online/thinking/error）
THEMES = {
    "nightblue": {
        "card": "#1c1f26", "input_bg": "#14161b",
        "bubble_me": "#2b5cff", "bubble_ai": "#23262f",
        "text_main": "#d8dbe2", "text_sub": "#9aa3b2", "text_faint": "#5b6270",
        "border": "#333", "hover": "#2a2d35", "pressed": "#20242c",
        "btn_bg": "#23262f", "scrollbar": "#333a48",
        "accent": "#2b5cff", "accent_hover": "#3d6bff", "accent_pressed": "#2452d8",
        "state": {"idle": "#60a5fa", "online": "#34d399", "thinking": "#fbbf24", "error": "#f87171"},
    },
    "forest": {
        "card": "#182420", "input_bg": "#111a16",
        "bubble_me": "#0d9463", "bubble_ai": "#20302a",
        "text_main": "#d6e4dc", "text_sub": "#93a89c", "text_faint": "#54655b",
        "border": "#2c3d34", "hover": "#24352c", "pressed": "#1d2c24",
        "btn_bg": "#20302a", "scrollbar": "#31453a",
        "accent": "#0d9463", "accent_hover": "#16b178", "accent_pressed": "#0a7a51",
        "state": {"idle": "#5eead4", "online": "#34d399", "thinking": "#fbbf24", "error": "#f87171"},
    },
    "twilight": {
        "card": "#211d2e", "input_bg": "#181525",
        "bubble_me": "#8b5cf6", "bubble_ai": "#2b2740",
        "text_main": "#e2dced", "text_sub": "#a99cbb", "text_faint": "#645878",
        "border": "#37304a", "hover": "#2d2740", "pressed": "#251f36",
        "btn_bg": "#2b2740", "scrollbar": "#3f3856",
        "accent": "#8b5cf6", "accent_hover": "#9d73f8", "accent_pressed": "#7449d6",
        "state": {"idle": "#c4b5fd", "online": "#6ee7b7", "thinking": "#f9a8d4", "error": "#fda4af"},
    },
    "obsidian": {
        "card": "#121212", "input_bg": "#0c0c0c",
        "bubble_me": "#b8860b", "bubble_ai": "#1f1f1f",
        "text_main": "#e8e2d5", "text_sub": "#a89f8c", "text_faint": "#5f5949",
        "border": "#2e2e2e", "hover": "#232323", "pressed": "#1b1b1b",
        "btn_bg": "#1f1f1f", "scrollbar": "#3a352a",
        "accent": "#e5b567", "accent_hover": "#f0c47a", "accent_pressed": "#c99e4e",
        "state": {"idle": "#e5b567", "online": "#9ccc65", "thinking": "#ffd700", "error": "#ef5350"},
    },
}

# 墨玉金的金色 LED 上发送键文字需要深色才可读；其余主题白字
_ACCENT_TEXT = {"obsidian": "#1a1a1a"}


def current_theme() -> str:
    name = QSettings(ORG, APP).value(_THEME_KEY, "nightblue")
    return name if name in THEMES else "nightblue"


def set_theme(name: str) -> None:
    if name in THEMES:
        QSettings(ORG, APP).setValue(_THEME_KEY, name)


def token(key: str) -> str:
    """当前主题的色值。"""
    return THEMES[current_theme()][key]


def state_colors() -> dict:
    """当前主题的状态灯 4 色（机器人 LED / 光晕 / 粒子跟随）。"""
    return THEMES[current_theme()]["state"]


def accent_text() -> str:
    """强调色之上的可读文字色（墨玉金用深字，其余白字）。"""
    return _ACCENT_TEXT.get(current_theme(), "#fff")
