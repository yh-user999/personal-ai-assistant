"""离屏渲染机器人各状态 → PNG，供人工视觉验收（QT_QPA_PLATFORM=offscreen）。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, "/root/personal-ai-assistant/desktop")

from PySide6.QtWidgets import QApplication

from floating_ball import FloatingBall

app = QApplication([])
ball = FloatingBall()

# 推进几帧让相位/粒子非零
for _ in range(12):
    ball._tick()

def snap(name: str) -> None:
    pm = ball.grab()
    pm.save(f"/tmp/robot_{name}.png")
    print("saved", name)

snap("idle")

ball.set_state("online")
for _ in range(8):
    ball._tick()
snap("online")

ball.set_state("thinking")
for _ in range(10):
    ball._tick()
snap("thinking")

ball.set_state("error")
ball._tick()
snap("error")

ball.set_state("online")
ball._dragging = True
ball._tick()
snap("dragging")

ball._dragging = False
ball.wave()
for _ in range(6):
    ball._tick()
snap("wave")
