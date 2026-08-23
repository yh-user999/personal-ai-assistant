# Personal AI Assistant

个人智能助手 —— Windows 本地 + 服务器混合部署，桌面悬浮机器人形态，
实现「记忆 → 分析 → 学习」闭环的个人工作助手。

## 架构总览

```
┌─ Windows 本地 ─────────────────────────────┐
│ ① collector/   行为采集器（前台窗口/浏览器/git） │
│ ② desktop/     PySide6 桌面悬浮球             │
└────────────────────────────────────────────┘
        ↕ Tailscale / frp 组网
┌─ JD 服务器 ────────────────────────────────┐
│ ③ server/      FastAPI 服务（记忆/分析/周报） │
└────────────────────────────────────────────┘
```

## 模块说明

| 目录 | 说明 | 技术栈 |
|------|------|--------|
| `server/` | 服务端：聊天编排、记忆闭环、行为统计、周报 | FastAPI + SQLite + sqlite-vec + DeepSeek API |
| `collector/` | Windows 采集器：前台窗口、浏览器历史、git 提交 | Python + Win32 API（ctypes） |
| `desktop/` | 桌面悬浮球：悬浮图标、聊天气泡、托盘、通知 | PySide6（Qt6） |
| `docs/` | 实施方案细则、架构文档 | Markdown |
| `scripts/` | 部署脚本（服务器 / 采集器 / 打包） | bash / PowerShell |

## 快速开始

```bash
# 1. 服务器端（JD 服务器）
cd server && pip install -r requirements.txt
cp .env.example ../.env   # 填入 DeepSeek / Embedding API Key
python run.py             # 启动 http://<host>:8000

# 2. 采集器（Windows）
cd collector && pip install -r requirements.txt
python main.py            # 采集并推送事件到服务器

# 3. 桌面悬浮球（Windows）
cd desktop && pip install -r requirements.txt
python main.py
```

详细实施步骤见 [`docs/实施方案细则.md`](docs/实施方案细则.md)。
设计参考来源与深入学习指南见 [`docs/REFERENCES.md`](docs/REFERENCES.md)。

## 许可证

私有项目，代码仅供本人使用。
