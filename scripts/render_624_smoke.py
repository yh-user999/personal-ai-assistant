"""离屏冒烟：6.24 桌面改动（面板标题 v4.9 + 托盘提醒 worker/定时器）。

用法: python scripts/render_624_smoke.py
输出: scripts/preview_out/624_panel.png, 624_tray.png + 控制台断言结果。
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

from chat_panel import ChatPanel  # noqa: E402
from floating_ball import FloatingBall  # noqa: E402
from tray import TrayIcon  # noqa: E402

failures = []

# ① 面板渲染 + 标题版本断言
ball = FloatingBall()
panel = ChatPanel(ball=ball)
panel.show()
app.processEvents()
title_label = panel._title
title_text = title_label.text()
print(f"面板标题: {title_text!r}")
if "v5.2" not in title_text:
    failures.append(f"面板标题未更新为 v5.2: {title_text!r}")
panel.grab().save(str(OUT / "624_panel.png"))
print(f"面板已渲染 → {OUT / '624_panel.png'}")

# ② 托盘：构造 + 情绪轮询定时器挂载断言（6.24 的提醒轮询已于第 8 课退役，改 QQ 推送）
tray = TrayIcon(ball)
assert tray._mood_timer is not None, "情绪轮询定时器未挂载"
assert tray._mood_timer.interval() == 60000, "情绪轮询间隔应为 60 秒"
assert not hasattr(tray, "_reminder_timer"), "提醒轮询应已退役（第 8 课改 QQ 推送）"
print(f"托盘情绪轮询: 间隔 {tray._mood_timer.interval() // 1000}s ✓（提醒轮询已退役）")
tray_icon_pix = tray.icon().pixmap(64, 64)
tray_icon_pix.save(str(OUT / "624_tray.png"))
print(f"托盘图标已渲染 → {OUT / '624_tray.png'}")

# ③ 像素抽查：面板/托盘图标都不是全空白
for name, pix in (("panel", panel.grab().toImage()), ("tray", tray_icon_pix.toImage())):
    nonempty = 0
    for x in range(0, pix.width(), max(1, pix.width() // 16)):
        for y in range(0, pix.height(), max(1, pix.height() // 16)):
            if pix.pixelColor(x, y).alpha() > 0:
                nonempty += 1
    print(f"{name} 不透明采样点: {nonempty}")
    if nonempty == 0:
        failures.append(f"{name} 渲染结果全空白")

# ④ 机器人本体回归（改动涉及 tray 同模块，保底确认没画坏）
ball.show()
app.processEvents()
ball.grab().save(str(OUT / "624_ball.png"))

if failures:
    print("\n❌ 离屏冒烟失败:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\n✅ 离屏冒烟全部通过")
