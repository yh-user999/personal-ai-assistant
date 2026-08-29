"""离屏冒烟：6.28 C2 体贴模式（caring 状态渲染 + 托盘情绪轮询挂载）。

用法: python scripts/render_627_smoke.py
输出: scripts/preview_out/627_caring_*.png + 控制台断言结果。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("EXECUTOR_ALLOWED_ROOTS", "C:/")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT))

OUT = ROOT / "scripts" / "preview_out"
OUT.mkdir(exist_ok=True)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from floating_ball import FloatingBall  # noqa: E402
from skins import SKIN_NAMES, set_skin  # noqa: E402
from tray import TrayIcon  # noqa: E402

failures = []

ball = FloatingBall()
ball.set_state("online")
ball.show()
app.processEvents()

# ① 托盘情绪轮询挂载断言
tray = TrayIcon(ball)
assert tray._mood_timer is not None, "情绪轮询定时器未挂载"
assert tray._mood_timer.interval() == 60000, "情绪轮询间隔应为 60 秒"
print(f"托盘情绪轮询: 间隔 {tray._mood_timer.interval() // 1000}s ✓")

# ② caring 状态切换 + 主色变化断言（三皮肤各渲一张）
from PySide6.QtGui import QColor  # noqa: E402

for skin in SKIN_NAMES:
    set_skin(skin)
    ball.set_state("online")
    app.processEvents()
    online_c = ball._state_color()
    ball.set_state("caring")
    app.processEvents()
    caring_c = ball._state_color()
    changed = online_c.name() != caring_c.name()
    print(f"{skin}: online={online_c.name()} caring={caring_c.name()} 变色={'✓' if changed else '✗'}")
    if not changed:
        failures.append(f"{skin} caring 未变色")
    ball.grab().save(str(OUT / f"627_caring_{skin}.png"))

    # 像素抽查：caring 暖色（R 显著高于 B）出现在渲染里
    img = ball.grab().toImage()
    warm = 0
    for x in range(0, img.width(), 3):
        for y in range(0, img.height(), 3):
            c = img.pixelColor(x, y)
            if c.alpha() > 0 and c.red() > c.blue() + 40:
                warm += 1
    print(f"{skin} 暖色像素采样: {warm}")
    if warm == 0:
        failures.append(f"{skin} caring 渲染无暖色像素")

# ③ 恢复：streak 结束 → 回 online（模拟 _on_mood 逻辑）
ball.set_state("caring")
tray._on_mood({"streak_active": False, "today_text": ""})
assert ball.state == "online", f"streak 结束未恢复 online（当前 {ball.state}）"
print("streak 结束恢复 online ✓")

# ④ 服务器不可达：保持现状不闪状态
ball.set_state("online")
tray._on_mood(None)
assert ball.state == "online", "None 载荷不应改变状态"
print("服务器不可达保持现状 ✓")

if failures:
    print("\n❌ 离屏冒烟失败:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\n✅ 离屏冒烟全部通过")
