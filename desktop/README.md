# Desktop —— PySide6 桌面悬浮球

桌面角落的半透明机器人图标（可拖拽），点击展开聊天气泡面板，托盘常驻。

## 功能

| 组件 | 文件 | 说明 |
|------|------|------|
| 悬浮球 | `floating_ball.py` | 无边框透明圆形图标，可拖拽，点击展开 |
| 聊天面板 | `chat_panel.py` | 气泡聊天窗，接入服务器 `/api/chat` |
| 托盘 | `tray.py` | 系统托盘：打开面板/今日概览/周报/退出 |
| API 客户端 | `api_client.py` | 服务器 HTTP 客户端（httpx） |

## 运行

```bash
pip install -r requirements.txt
python main.py
```

## 打包 exe

```powershell
# 见 scripts/build_desktop.ps1
pyinstaller --noconfirm --onefile --windowed main.py
```

## 实现状态

- [x] 目录骨架
- [ ] M3 悬浮球 + 聊天面板 + 托盘
