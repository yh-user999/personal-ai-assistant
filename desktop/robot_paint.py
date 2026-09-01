"""机器人绘制的共享图元。

背景：floating_ball.py（714 行）、robot_avatar.py（314 行）、tray.py（305 行）
各自实现了同样的 bender/astro/classic 三套造型，`_draw_hex` 在前两者里
逐字重复，tray 的图标已经漂移（不再带呼吸/状态色联动）。

这里只抽**几何图元**（与部件尺寸无关的纯绘制），不抽整套造型：
造型代码是逐像素调过的，三处的画布尺寸/比例各不相同，强行统一签名风险
远大于收益，而且桌面端没有可自动化的视觉回归测试。图元层统一后，
"改一处螺栓样式要同步三个文件"的问题已经解决，造型本身留在各自文件里。
"""
import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen


def draw_hex_bolt(
    painter: QPainter,
    cx: float,
    cy: float,
    r: float,
    fill,
    stroke: QColor,
) -> None:
    """六角螺栓（平边朝上），中心带压痕点。

    fill 接受 QColor 或 QLinearGradient（Qt 的 setBrush 都能吃）。
    """
    path = QPainterPath()
    for i in range(6):
        ang = math.radians(60 * i)
        px = cx + r * math.cos(ang)
        py = cy + r * math.sin(ang)
        if i == 0:
            path.moveTo(px, py)
        else:
            path.lineTo(px, py)
    path.closeSubpath()
    painter.setBrush(fill)
    painter.setPen(QPen(stroke, 1))
    painter.drawPath(path)
    # 中心压痕：让螺栓有"拧过"的立体感
    painter.setBrush(QColor(0, 0, 0, 60))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(cx, cy), r * 0.32, r * 0.32)


def state_color(state: str, tokens: dict) -> QColor:
    """状态灯颜色：online 绿 / thinking 琥珀 / offline 红。

    tokens 传 theme.token("state")，保持与主题一致；未知状态回退 offline。
    抽出来是为了让 tray 图标也能用上同一套状态色（此前 tray 写死了颜色，
    与悬浮球的状态灯不联动）。
    """
    key = state if state in ("online", "thinking", "offline") else "offline"
    value = tokens.get(key)
    return QColor(value) if value else QColor("#888888")


def breath_scale(phase: float, amplitude: float = 0.03) -> float:
    """呼吸缩放系数：phase 为 0~2π 的相位，返回 1±amplitude 的缩放。

    三处载体的呼吸动画都是这个公式，参数不同而已。
    """
    return 1.0 + amplitude * math.sin(phase)
