"""第 14 课测试：机器人四肢姿态计算（纯数学，无 Qt 依赖）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

from robot_pose import arm_angle, leg_angles


def test_idle_arms_symmetric_sway():
    assert abs(arm_angle("online", 0.0, False, False, -1, "L") - -18) < 1e-6
    assert abs(arm_angle("online", 0.0, False, False, -1, "R") - 18) < 1e-6
    # 随呼吸相位轻微摆动（sin(1.57)=1 → 外展增大）
    assert arm_angle("online", 1.57, False, False, -1, "R") > 18
    assert arm_angle("online", 4.71, False, False, -1, "R") < 18


def test_thinking_pose_hand_to_chin():
    assert arm_angle("thinking", 0.0, False, False, -1, "R") < -60  # 右手上举托下巴
    assert arm_angle("thinking", 0.0, False, False, -1, "L") < 0  # 左手保持自然


def test_dragging_arms_up():
    for side in ("L", "R"):
        assert arm_angle("online", 0.0, True, False, -1, side) < -140  # 被拎起来：双臂上举


def test_wave_oscillates():
    angles = {arm_angle("online", 0.0, False, True, p, "R") for p in (0.05, 0.12, 0.15)}
    assert len(angles) == 3  # 不同进度角度不同（在摆）
    assert all(-150 < a < -85 for a in angles)
    # 未招手（progress<0）时回到自然位
    assert arm_angle("online", 0.0, False, True, -1, "R") == 18


def test_legs_swing_larger_when_dragging():
    idle = leg_angles(1.57, False)
    drag = leg_angles(1.57, True)
    assert abs(drag[0]) > abs(idle[0])
    assert idle[0] < 0 < idle[1]  # 左右对称摆动
