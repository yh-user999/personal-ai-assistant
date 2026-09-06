"""悬浮球皮肤绘制函数注册表（自 floating_ball.py 迁出，像素级等价）。

新增皮肤 = 在本模块加一个 `paint_xxx(ball, painter, accent)` 并登记进 PAINTERS，
不再需要改 FloatingBall 本体。`ball` 只读以下状态：state / _phase / _blink /
_wave / _dragging（与 floating_ball.FloatingBall 的动画状态字段耦合）。
视觉预览：python scripts/render_robot_preview.py（产物在 scripts/preview_out/）。
"""
import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen

import robot_paint
from robot_pose import arm_angle, leg_angles


def draw_limb(
    painter: QPainter,
    x: float,
    y: float,
    length: float,
    angle_deg: float,
    width: float,
    color: QColor,
) -> None:
    """画一条圆头肢体：从 (x,y) 出发，角度相对垂直向下（正=顺时针）。"""
    rad = math.radians(angle_deg)
    ex = x + length * math.sin(rad)
    ey = y + length * math.cos(rad)
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(int(x), int(y), int(ex), int(ey))
    return ex, ey


def shell_gradient(x: float, y: float, w: float, h: float) -> QLinearGradient:
    """机身渐变：金属灰——左上亮 → 右下暗（班德式机械质感）。"""
    g = QLinearGradient(x, y, x + w, y + h)
    g.setColorAt(0.0, QColor("#a9b2c4"))
    g.setColorAt(0.45, QColor("#6b7488"))
    g.setColorAt(1.0, QColor("#3a414e"))
    return g


def head_path() -> QPainterPath:
    """硬核机械风头部：平顶 + 45° 切角的工业外壳轮廓（宽度收敛，给身体留比重）。"""
    p = QPainterPath()
    p.moveTo(24, 16)     # 顶边左端（平顶）
    p.lineTo(56, 16)
    p.lineTo(61, 21)     # 右上切角
    p.lineTo(61, 47)
    p.lineTo(56, 52)     # 右下切角
    p.lineTo(24, 52)
    p.lineTo(19, 47)
    p.lineTo(19, 21)
    p.closeSubpath()
    return p


def paint_particles(ball, painter: QPainter, accent: QColor) -> None:
    """思考时环绕粒子（3 个小光点绕头转，三套皮肤共享）。"""
    if ball.state != "thinking":
        return
    painter.setPen(Qt.NoPen)
    for i in range(3):
        ang = ball._phase * 1.8 + i * 2.094
        px = 40 + 16 * math.cos(ang)
        py = 33 + 16 * math.sin(ang)
        a = int(110 + 90 * math.sin(ball._phase * 3 + i))
        dot = QColor(accent)
        dot.setAlpha(max(0, a))
        painter.setBrush(dot)
        painter.drawEllipse(int(px), int(py), 3, 3)


# ── 班德金属风 · 硬核机械版（平顶切角外壳 + 内嵌小屏 + 散热格栅）──
# 六角螺栓用 robot_paint.draw_hex_bolt（与 robot_avatar 共用）。

def paint_bender(ball, painter: QPainter, accent: QColor) -> None:
    limb_color = QColor("#454c5a")
    stroke = QColor("#525a6b")
    dark = QColor("#12151c")      # 屏幕内芯
    recess = QColor("#232833")    # 内凹底座
    steel = shell_gradient(19, 16, 42, 36)

    # 腿（先画，被身体压住根部）
    l_leg, r_leg = leg_angles(ball._phase, ball._dragging)
    for hip_x, ang in ((34, l_leg), (46, r_leg)):
        fx, fy = draw_limb(painter, hip_x, 64, 12, ang, 3.5, limb_color)
        painter.setPen(Qt.NoPen)
        painter.setBrush(limb_color)
        painter.drawEllipse(int(fx - 2.5), int(fy - 1.5), 5, 4)

    # 手臂
    waving = ball._wave >= 0
    for side, sh_x in (("L", 29), ("R", 51)):
        ang = arm_angle(ball.state, ball._phase, ball._dragging, waving, ball._wave, side)
        hx, hy = draw_limb(painter, sh_x, 56, 15, ang, 3.5, limb_color)
        painter.setPen(Qt.NoPen)
        painter.setBrush(limb_color)
        painter.drawEllipse(int(hx - 2.5), int(hy - 2.5), 5, 5)

    # 侧面六角螺栓（机械感侧耳）
    for bx in (13.5, 66.5):
        robot_paint.draw_hex_bolt(painter, bx, 34, 6, steel, stroke)

    # 颈部（方钢）
    painter.setBrush(QColor("#2e3440"))
    painter.setPen(Qt.NoPen)
    painter.drawRect(34, 50, 12, 6)

    # 身体（方直外壳 + 横向拼缝）
    painter.setBrush(shell_gradient(27, 54, 26, 13))
    painter.setPen(QPen(stroke, 1))
    painter.drawRoundedRect(27, 54, 26, 13, 2, 2)
    painter.setPen(QPen(QColor(0, 0, 0, 70), 1))
    painter.drawLine(28, 61, 52, 61)

    # 胸口检修屏 + 状态脉冲
    painter.setBrush(recess)
    painter.setPen(Qt.NoPen)
    painter.drawRect(35, 56, 10, 6)
    pulse = QColor(accent)
    pulse.setAlpha(int(140 + 100 * math.sin(ball._phase * 2)))
    painter.setBrush(pulse)
    painter.drawEllipse(39, 58, 3, 3)

    # 天线（方形基座 + 状态光点）
    painter.setPen(QPen(stroke, 2))
    painter.drawLine(40, 16, 40, 8)
    glow = QColor(accent)
    glow.setAlpha(int(70 + 70 * math.sin(ball._phase * 2)))
    painter.setBrush(glow)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(36, 1, 8, 8)
    painter.setBrush(accent)
    painter.drawRect(38, 3, 4, 4)

    # 头（平顶切角外壳）
    painter.setBrush(steel)
    painter.setPen(QPen(stroke, 1.2))
    painter.drawPath(head_path())

    # 顶板拼缝 + 铆钉 + 顶缘高光
    painter.setPen(QPen(QColor(0, 0, 0, 80), 1))
    painter.drawLine(22, 22, 58, 22)
    painter.setBrush(QColor("#2e3440"))
    painter.drawEllipse(QPointF(24.5, 19), 1.2, 1.2)
    painter.drawEllipse(QPointF(55.5, 19), 1.2, 1.2)
    painter.setPen(QPen(QColor(255, 255, 255, 46), 1.5, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(26, 17.5, 54, 17.5)

    # 内嵌显示屏（只占头宽 1/3——脸不再喧宾夺主）
    painter.setBrush(recess)
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(26, 26, 28, 11, 2, 2)
    painter.setBrush(dark)
    painter.drawRoundedRect(27, 27, 26, 9, 1, 1)

    # LED 扫描眼（分段 LED 组；眨眼 = 高度压扁；thinking = 加辉光）
    eye_h = 4.0 * (1.0 - 0.85 * abs(math.sin(ball._blink * math.pi)))
    eye_h = max(1.6, eye_h)
    eye_y = 31.5 - eye_h / 2
    if ball.state == "thinking":
        halo = QColor(accent)
        halo.setAlpha(60)
        painter.setBrush(halo)
        painter.drawRoundedRect(28, 28, 24, 7, 2, 2)
    painter.setBrush(QColor(accent))
    painter.drawRoundedRect(29, eye_y, 22, eye_h, 1, 1)
    painter.setPen(QPen(QColor(0, 0, 0, 130), 1))
    painter.drawLine(36, eye_y + 0.8, 36, eye_y + eye_h - 0.8)
    painter.drawLine(44, eye_y + 0.8, 44, eye_y + eye_h - 0.8)

    # 下颚散热格栅（竖栅）
    painter.setBrush(recess)
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(32, 40, 16, 7, 1, 1)
    painter.setPen(QPen(QColor("#565e70"), 1.4))
    for gx in (34.5, 37.5, 40.5, 43.5, 46.5):
        painter.drawLine(gx, 41, gx, 46)

    paint_particles(ball, painter, accent)


# ── 白色宇航员风 · 重制版（圆球头盔 + 深色玻璃面罩 + 发光圆眼）──

def paint_astro(ball, painter: QPainter, accent: QColor) -> None:
    stroke = QColor("#9aa5b8")
    yellow = QColor("#f5c518")
    yellow_dark = QColor("#c99e14")

    # 腿：白色短腿 + 黄色靴子（荡腿 = 左右微摆）
    l_leg, r_leg = leg_angles(ball._phase, ball._dragging)
    for cx_leg, ang in ((33, l_leg), (47, r_leg)):
        dx = int(ang * 0.25)
        painter.setBrush(QColor("#e2e7ef"))
        painter.setPen(QPen(stroke, 1))
        painter.drawRoundedRect(cx_leg - 3.5 + dx, 62, 7, 12, 3, 3)
        painter.setBrush(yellow)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(cx_leg - 4.5 + dx, 72, 9, 4.5, 2, 2)

    # 手臂：白管臂 + 圆手套（招手/托下巴/上举姿势复用）
    waving = ball._wave >= 0
    for side, sh_x in (("L", 26), ("R", 54)):
        ang = arm_angle(ball.state, ball._phase, ball._dragging, waving, ball._wave, side)
        hx, hy = draw_limb(painter, sh_x, 55, 14, ang, 5, QColor("#dfe4ec"))
        painter.setPen(QPen(stroke, 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QPointF(hx, hy), 4.5, 4.5)

    # 身体：白宇航服 + 黄色舷窗描边 + 状态核心灯
    painter.setBrush(QColor("#f4f6fa"))
    painter.setPen(QPen(stroke, 1))
    painter.drawRoundedRect(30, 52, 20, 13, 5, 5)
    painter.setPen(QPen(yellow, 1.4))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(33, 55, 14, 7, 3, 3)
    pulse = QColor(accent)
    pulse.setAlpha(int(150 + 90 * math.sin(ball._phase * 2)))
    painter.setBrush(pulse)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(40, 58.5), 2, 2)

    # 天线（小月签名）
    painter.setPen(QPen(stroke, 2))
    painter.drawLine(40, 11, 40, 5)
    glow = QColor(accent)
    glow.setAlpha(int(70 + 70 * math.sin(ball._phase * 2)))
    painter.setBrush(glow)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(36.5, 0.5, 7, 7)
    painter.setBrush(accent)
    painter.drawEllipse(QPointF(40, 4), 2.5, 2.5)

    # 头盔：正圆球体 + 底部体积阴影 + 顶部高光
    helmet_g = QLinearGradient(22, 12, 58, 50)
    helmet_g.setColorAt(0.0, QColor("#ffffff"))
    helmet_g.setColorAt(0.6, QColor("#e6ebf3"))
    helmet_g.setColorAt(1.0, QColor("#c2cad8"))
    painter.setBrush(helmet_g)
    painter.setPen(QPen(stroke, 1.2))
    painter.drawEllipse(QPointF(40, 31), 21, 21)
    painter.setPen(QPen(QColor(70, 80, 100, 60), 3, Qt.SolidLine, Qt.RoundCap))
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(23, 14, 34, 34, 30 * 16, 120 * 16)
    painter.setPen(QPen(QColor(255, 255, 255, 200), 3, Qt.SolidLine, Qt.RoundCap))
    painter.drawArc(25, 16, 30, 26, 170 * 16, 100 * 16)

    # 侧耳灯（黄色圆灯）
    painter.setBrush(yellow)
    painter.setPen(QPen(yellow_dark, 1))
    painter.drawEllipse(QPointF(19, 31), 4.5, 4.5)
    painter.drawEllipse(QPointF(61, 31), 4.5, 4.5)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#ffefb8"))
    painter.drawEllipse(QPointF(18, 29.8), 1.5, 1.5)
    painter.drawEllipse(QPointF(60, 29.8), 1.5, 1.5)

    # 面罩：深色玻璃航太镜片 + 顶缘内高光
    visor_g = QLinearGradient(28, 21, 52, 42)
    visor_g.setColorAt(0.0, QColor("#2b3550"))
    visor_g.setColorAt(1.0, QColor("#151b29"))
    painter.setBrush(visor_g)
    painter.setPen(QPen(QColor("#10141f"), 1))
    painter.drawRoundedRect(28, 21, 24, 21, 8, 8)
    painter.setPen(QPen(QColor(255, 255, 255, 36), 1.2, Qt.SolidLine, Qt.RoundCap))
    painter.setBrush(Qt.NoBrush)
    painter.drawLine(31, 23.5, 49, 23.5)

    # 玻璃斜向反光带
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(255, 255, 255, 30))
    streak = QPainterPath()
    streak.moveTo(31.5, 22)
    streak.lineTo(35, 22)
    streak.lineTo(30, 40)
    streak.lineTo(28, 36)
    streak.closeSubpath()
    painter.drawPath(streak)
    painter.setBrush(QColor(255, 255, 255, 16))
    streak2 = QPainterPath()
    streak2.moveTo(38, 22)
    streak2.lineTo(40, 22)
    streak2.lineTo(34, 41)
    streak2.lineTo(32, 41)
    streak2.closeSubpath()
    painter.drawPath(streak2)

    # 眼睛：玻璃后的发光圆眼（眨眼 = 横线；error = 灰线）
    eye_y = 30.0 if ball.state != "thinking" else 28.0
    eye_core = QColor("#e8f4ff") if ball.state != "error" else QColor("#8b93a3")
    for ex in (35.5, 44.5):
        halo = QColor(accent)
        halo.setAlpha(55)
        painter.setBrush(halo)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(ex, eye_y), 4, 4)
    if ball._blink > 0 or ball.state == "error":
        painter.setPen(QPen(eye_core, 1.8, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(33, int(eye_y), 38, int(eye_y))
        painter.drawLine(42, int(eye_y), 47, int(eye_y))
    else:
        painter.setBrush(eye_core)
        painter.drawEllipse(QPointF(35.5, eye_y), 2.2, 3.4)
        painter.drawEllipse(QPointF(44.5, eye_y), 2.2, 3.4)
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.drawEllipse(QPointF(34.8, eye_y - 1.4), 0.9, 0.9)
        painter.drawEllipse(QPointF(43.8, eye_y - 1.4), 0.9, 0.9)
    if ball.state == "thinking" and ball._blink == 0:
        # 四角星闪点（thinking 专属小星星）
        painter.setBrush(QColor(255, 255, 255, 220))
        sx, sy, r1 = 48.5, 25.5, 2.2
        star = QPainterPath()
        star.moveTo(sx, sy - r1)
        star.quadTo(sx, sy, sx + r1, sy)
        star.quadTo(sx, sy, sx, sy + r1)
        star.quadTo(sx, sy, sx - r1, sy)
        star.quadTo(sx, sy, sx, sy - r1)
        painter.drawPath(star)

    # 嘴：微笑弧（thinking = 圆圆的 o；error = 嘴角向下）
    painter.setPen(QPen(QColor("#dfe9ff"), 1.3, Qt.SolidLine, Qt.RoundCap))
    painter.setBrush(Qt.NoBrush)
    if ball.state == "thinking":
        painter.drawEllipse(QPointF(40, 36.5), 1.6, 1.6)
    elif ball.state == "error":
        painter.drawArc(37, 33, 6, 5, 20 * 16, 140 * 16)
    else:
        painter.drawArc(37, 34, 6, 4, 200 * 16, 140 * 16)

    paint_particles(ball, painter, accent)


# ── 原版萌系风（暗色圆角头 + 双 LED 大眼 + 微笑/腮红 + 胸口屏）──

def paint_classic(ball, painter: QPainter, accent: QColor) -> None:
    limb_color = QColor("#3f4654")
    stroke = QColor("#4a5264")
    dark_g = QLinearGradient(14, 14, 62, 52)
    dark_g.setColorAt(0.0, QColor("#4a5266"))
    dark_g.setColorAt(0.45, QColor("#2c313d"))
    dark_g.setColorAt(1.0, QColor("#1a1d24"))

    # 腿
    l_leg, r_leg = leg_angles(ball._phase, ball._dragging)
    for hip_x, ang in ((34, l_leg), (46, r_leg)):
        fx, fy = draw_limb(painter, hip_x, 62, 13, ang, 3.5, limb_color)
        painter.setPen(Qt.NoPen)
        painter.setBrush(limb_color)
        painter.drawEllipse(int(fx - 2.5), int(fy - 1.5), 5, 4)

    # 手臂
    waving = ball._wave >= 0
    for side, sh_x in (("L", 28), ("R", 52)):
        ang = arm_angle(ball.state, ball._phase, ball._dragging, waving, ball._wave, side)
        hx, hy = draw_limb(painter, sh_x, 53, 16, ang, 3.5, limb_color)
        painter.setPen(Qt.NoPen)
        painter.setBrush(limb_color)
        painter.drawEllipse(int(hx - 2.5), int(hy - 2.5), 5, 5)

    # 耳朵（头部两侧小侧板）
    painter.setBrush(dark_g)
    painter.setPen(QPen(stroke, 1))
    painter.drawRoundedRect(11, 26, 8, 13, 4, 4)
    painter.drawRoundedRect(61, 26, 8, 13, 4, 4)

    # 身体
    painter.setBrush(dark_g)
    painter.setPen(QPen(stroke, 1))
    painter.drawRoundedRect(28, 50, 24, 14, 7, 7)

    # 胸口小屏 + 状态色脉冲点
    painter.setBrush(QColor("#14161c"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(35, 55, 10, 5, 2, 2)
    pulse = QColor(accent)
    pulse.setAlpha(int(140 + 100 * math.sin(ball._phase * 2)))
    painter.setBrush(pulse)
    painter.drawEllipse(39, 56, 3, 3)

    # 天线（脉冲光点）
    painter.setPen(QPen(QColor("#4b5563"), 2))
    painter.drawLine(40, 14, 40, 7)
    glow = QColor(accent)
    glow.setAlpha(int(70 + 70 * math.sin(ball._phase * 2)))
    painter.setBrush(glow)
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(35, 0, 10, 10)
    painter.setBrush(accent)
    painter.drawEllipse(37, 2, 6, 6)

    # 头部（暗色圆角）
    painter.setBrush(dark_g)
    painter.setPen(QPen(stroke, 1))
    painter.drawRoundedRect(18, 14, 44, 38, 12, 12)

    # 顶部高光弧
    painter.setPen(QPen(QColor(255, 255, 255, 34), 3, Qt.SolidLine, Qt.RoundCap))
    painter.drawArc(24, 18, 20, 12, 180 * 16, 180 * 16)

    # 双 LED 大眼（光晕 + 高光；眨眼 = 高度压扁）
    eye_h = 11.0 * (1.0 - 0.85 * abs(math.sin(ball._blink * math.pi)))
    eye_w = 11.0
    outer = QColor(accent)
    outer.setAlpha(55)
    painter.setBrush(outer)
    painter.setPen(Qt.NoPen)
    for ex in (26, 43):
        painter.drawEllipse(int(ex - 3), int(24 + (11 - eye_h) / 2), int(eye_w + 6), int(eye_h + 6))
    painter.setBrush(QColor(accent))
    for ex in (26, 43):
        painter.drawEllipse(int(ex), int(27 + (11 - eye_h) / 2), int(eye_w), max(2, int(eye_h)))
    if ball._blink == 0:
        painter.setBrush(QColor(255, 255, 255, 170))
        for ex in (29, 46):
            painter.drawEllipse(ex, 29, 3, 3)

    # 腮红（error 时消失）
    if ball.state != "error":
        painter.setBrush(QColor(255, 122, 156, 74))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(22, 40, 6, 3.5)
        painter.drawEllipse(52, 40, 6, 3.5)

    # 嘴巴（微笑 / 思考圆 / 难过）
    painter.setPen(QPen(QColor("#9aa3b5"), 2, Qt.SolidLine, Qt.RoundCap))
    painter.setBrush(Qt.NoBrush)
    if ball.state == "thinking":
        painter.drawEllipse(37, 42, 6, 6)
    elif ball.state == "error":
        painter.drawArc(34, 40, 12, 9, 20 * 16, 140 * 16)  # 嘴角向下
    else:
        painter.drawArc(34, 39, 12, 9, 200 * 16, 140 * 16)

    paint_particles(ball, painter, accent)


# 皮肤名 → 绘制函数；新增皮肤在此登记即可（skins.SKIN_NAMES 同步加名字）。
PAINTERS = {
    "bender": paint_bender,
    "astro": paint_astro,
    "classic": paint_classic,
}
