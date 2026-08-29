"""离屏冒烟：6.26 消息全文检索（面板 v5.0 标题 + 检索弹窗渲染）。

用法: python scripts/render_626_smoke.py
输出: scripts/preview_out/626_panel.png, 626_search.png + 控制台断言结果。
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

from chat_panel import ChatPanel, _SearchDialog  # noqa: E402
from floating_ball import FloatingBall  # noqa: E402

failures = []

ball = FloatingBall()
panel = ChatPanel(ball=ball)
panel.show()
app.processEvents()

# ① 面板标题版本断言
title_text = panel._title.text()
print(f"面板标题: {title_text!r}")
if "v5.0" not in title_text:
    failures.append(f"面板标题未更新为 v5.0: {title_text!r}")
panel.grab().save(str(OUT / "626_panel.png"))
print(f"面板已渲染 → {OUT / '626_panel.png'}")

# ② 检索弹窗：直接喂结果数据（不依赖服务器），验证渲染 + 展示逻辑
dlg = _SearchDialog(panel.client, parent=panel)
dlg.show()
app.processEvents()
sample = {
    "query": "田地",
    "total": 2,
    "results": [
        {"id": 1, "sender": "user", "sender_name": "你", "ts_local": "08-20 10:00",
         "snippet": "李羽家的田地是三四亩，市价八到十二两"},
        {"id": 2, "sender": "assistant", "sender_name": "小月", "ts_local": "08-20 10:01",
         "snippet": "已记住：李羽家 田地 三四亩"},
    ],
}
dlg.show_results(sample)
app.processEvents()
text = dlg.results.toPlainText()
print(f"弹窗结果区文本（前80字）: {text[:80]!r}")
if "共 2 条命中" not in text or "三四亩" not in text:
    failures.append(f"结果区内容不符: {text[:120]!r}")

# 无结果分支
dlg.show_results({"query": "xyz", "total": 0, "results": []})
app.processEvents()
assert "没有找到" in dlg.results.toPlainText(), "无结果分支渲染失败"
print("无结果分支 ✓")

# 请求失败分支
dlg.show_results(None)
app.processEvents()
assert "检索请求失败" in dlg.results.toPlainText(), "失败分支渲染失败"
print("失败分支 ✓")

dlg.grab().save(str(OUT / "626_search.png"))
print(f"弹窗已渲染 → {OUT / '626_search.png'}")

# ③ 像素抽查：面板/弹窗都不是全空白
for name, pix in (("panel", panel.grab().toImage()), ("search", dlg.grab().toImage())):
    nonempty = 0
    for x in range(0, pix.width(), max(1, pix.width() // 16)):
        for y in range(0, pix.height(), max(1, pix.height() // 16)):
            if pix.pixelColor(x, y).alpha() > 0:
                nonempty += 1
    print(f"{name} 不透明采样点: {nonempty}")
    if nonempty == 0:
        failures.append(f"{name} 渲染结果全空白")

if failures:
    print("\n❌ 离屏冒烟失败:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\n✅ 离屏冒烟全部通过")
