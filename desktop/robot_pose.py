"""机器人四肢姿态计算（第 14 课：纯数学模块，无 Qt 依赖，可单测）。

约定：角度为相对"垂直向下"方向的偏角（度），正 = 顺时针（向右摆）。
- 手臂：外展 >0，上举为负（-90 = 水平，-150 ≈ 举过头顶）
- 腿：左右对称摆动，负 = 左腿外摆
"""
import math


def arm_angle(
    state: str,
    phase: float,
    dragging: bool,
    waving: bool,
    wave_progress: float,
    side: str,
) -> float:
    """单侧手臂偏角。side: 'L'/'R'。"""
    if dragging:
        # 被拎起来：双臂上举（稍微不对称更自然）
        return -152 + (3 if side == "R" else -3)
    if waving and side == "R":
        if wave_progress < 0:
            return 18  # 招手结束回到自然位
        # 招手：右臂举到头顶附近快速左右摆（wave_progress 0→1 内摆 5 个来回）
        return -118 + 26 * math.sin(wave_progress * 10 * math.pi)
    if state == "thinking" and side == "R":
        return -72  # 右手托下巴（经典思考姿势）
    # 自然垂臂：轻微外展 + 随呼吸缓慢摆动
    sway = 6 * math.sin(phase)
    base = 18 if side == "R" else -18
    return base + sway


def leg_angles(phase: float, dragging: bool) -> tuple[float, float]:
    """双腿偏角 (左, 右)。拖拽时摆动幅度更大（像被拎起来荡腿）。"""
    amp = 12 if dragging else 4
    return (-amp * math.sin(phase * 1.3), amp * math.sin(phase * 1.3))
