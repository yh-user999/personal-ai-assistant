"""离屏渲染机器人各状态 → PNG，供人工视觉验收。

跨平台版：自动定位 desktop 目录，输出到 scripts/preview_out/。
用法:
  python scripts/render_robot_preview.py                 # 全部皮肤 × 状态 × 载体
  python scripts/render_robot_preview.py --skin bender   # 只渲指定皮肤
"""
import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))

OUT = ROOT / "scripts" / "preview_out"

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from floating_ball import FloatingBall  # noqa: E402
from robot_avatar import RobotAvatar  # noqa: E402
from skins import SKIN_NAMES  # noqa: E402
from tray import make_robot_icon  # noqa: E402


def snap_widget(widget, name: str, scale: int = 3) -> None:
    pm = QPixmap(widget.width() * scale, widget.height() * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(scale, scale)
    widget.render(p, QPoint(0, 0))
    p.end()
    pm.save(str(OUT / f"{name}.png"))
    print("saved", name)


def snap_icon(skin: str, size: int = 128) -> None:
    icon = make_robot_icon(size=size, skin=skin)
    pm = icon.pixmap(size, size)
    pm.save(str(OUT / f"tray_{skin}.png"))
    print("saved", f"tray_{skin}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skin", choices=list(SKIN_NAMES), help="只渲染指定皮肤")
    args = ap.parse_args()
    skins = [args.skin] if args.skin else list(SKIN_NAMES)

    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    ball = FloatingBall()

    for skin in skins:
        ball.skin = skin
        for frame in range(12):
            ball._tick()
        ball.set_state("idle")
        for frame in range(8):
            ball._tick()
        snap_widget(ball, f"ball_{skin}_idle")

        ball.set_state("thinking")
        for frame in range(10):
            ball._tick()
        snap_widget(ball, f"ball_{skin}_thinking")

        ball.set_state("error")
        ball._tick()
        snap_widget(ball, f"ball_{skin}_error")

        ball.set_state("online")
        ball._dragging = True
        ball._tick()
        snap_widget(ball, f"ball_{skin}_drag")
        ball._dragging = False

        ball.wave()
        for frame in range(6):
            ball._tick()
        snap_widget(ball, f"ball_{skin}_wave")

        # 迷你头像（聊天面板）
        avatar = RobotAvatar()
        avatar.skin = skin
        for frame in range(10):
            avatar._tick()
        snap_widget(avatar, f"avatar_{skin}")
        avatar.set_thinking(True)
        for frame in range(10):
            avatar._tick()
        snap_widget(avatar, f"avatar_{skin}_think")

        # 托盘图标
        snap_icon(skin)

    print("done →", OUT)


if __name__ == "__main__":
    main()
