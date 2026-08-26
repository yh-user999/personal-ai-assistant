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
| `desktop/` | 桌面悬浮机器人：自绘形象（呼吸/眨眼/状态灯）、气泡聊天、托盘、开机自启 | PySide6（Qt6） |
| `docs/` | 实施方案细则、架构文档 | Markdown |
| `scripts/` | 部署脚本（服务器 / 采集器 / 打包） | bash / PowerShell |

## 快速开始

```bash
# 0. 前置：两台机器装 Tailscale 并登录同一账号（服务走专线，不暴露公网）

# 1. 服务器端（JD 服务器）
cd server && pip install -r requirements.txt
cp .env.example ../.env   # 填入 LLM/Embedding Key + 生成 API_TOKEN
python run.py             # 启动后仅 Tailscale IP 可访问

# 2. 采集器（Windows）
cd collector && pip install -r requirements.txt
# .env: SERVER_URL=http://<服务器Tailscale IP>:8000 + API_TOKEN
python main.py

# 3. 桌面悬浮球（Windows）
cd desktop && pip install -r requirements.txt
python main.py
```

详细实施步骤见 [`docs/实施方案细则.md`](docs/实施方案细则.md)。
设计参考来源与深入学习指南见 [`docs/REFERENCES.md`](docs/REFERENCES.md)。
外部 AI 评审的采纳/拒绝记录见 [`docs/AI_REVIEW.md`](docs/AI_REVIEW.md)。
让 AI 继续优化的提问模板见 [`docs/AI_OPTIMIZATION_PROMPTS.md`](docs/AI_OPTIMIZATION_PROMPTS.md)。
部署环境与服务器信息见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。
实践日志与踩坑记录见 [`docs/LESSONS.md`](docs/LESSONS.md)。
六课带教进度账本见 [`docs/LEARNING_PROGRESS.md`](docs/LEARNING_PROGRESS.md)。
组件启停与排查命令见 [`docs/OPS.md`](docs/OPS.md)。
加"心智功能"的术语地图与检索方法见 [`docs/RESEARCH_GUIDE.md`](docs/RESEARCH_GUIDE.md)。
记忆/反思/人格/情绪的测试方法见 [`docs/TESTING_GUIDE.md`](docs/TESTING_GUIDE.md)。

## 安全

- 全 API 走 `API_TOKEN` 鉴权（.env 配置，不入库）
- 服务仅 Tailscale 专线可达，公网只暴露 SSH 22
- 敏感信息（IP/密钥/token）只存本地笔记，**禁止入仓库**

## 许可证

私有项目，代码仅供本人使用。
