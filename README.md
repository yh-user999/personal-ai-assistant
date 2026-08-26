# Personal AI Assistant 🤖

个人智能助手 —— Windows 本地 + 服务器混合部署，桌面悬浮机器人形态，实现「记忆 → 分析 → 学习」闭环的个人工作助手。

> 状态：六课带教全部完成，进阶课 9/10 已完成 · 59 个测试全绿 · 全自动运行中

## 能力总览

| 维度 | 能力 |
|------|------|
| **记忆** | 双层记忆（情景原文 + 三元组事实）、向量检索 + BM25 混合、遗忘衰减、术语词典、四维画像 |
| **反思** | 自省（纠正→教训→永久遵守）、每日小结（22:00）、每周学习反思（周日 21:00） |
| **感知** | 行为实时注入（当前窗口/git 提交/近 1h 活跃）、三通道采集（前台窗口/浏览器历史/git）、心跳健康 |
| **学习** | 关切追踪（在意什么）、风格学习（认可的回复形式）、多轮上下文（8 轮原文 + 摘要续接） |
| **RAG** | 文档知识库：切块 → 向量化 → 混合检索（RRF）→ 带引用回答；Hit@k/MRR 评测体系 |
| **交互** | 桌面悬浮机器人（自绘形象/呼吸眨眼/状态灯）、气泡聊天（Markdown 渲染）、托盘通知、双击开面板、📌 图钉 |
| **运维** | 5 个定时任务、每日 03:00 热备份（滚动 7 份）、开机自启 + 崩溃自愈、黑匣子日志 |

## 架构

```
┌─ Windows 本地 ──────────────────────────────────────┐
│ ② collector/ 行为采集器（窗口 8s / 浏览器 10min / git 15min）│
│    脱敏 → 攒批 → 幂等推送 → 心跳（5min）                    │
│ ③ desktop/   桌面悬浮机器人（PySide6）                       │
│    双击聊天 / 气泡面板 / 托盘 / 状态灯（在线·思考·断线）      │
└────────────────────────────────────────────────────┘
        ↕ Tailscale 加密专线（公网只暴露 SSH 22）
┌─ JD 服务器 ─────────────────────────────────────────┐
│ ① server/    FastAPI 单进程（uvicorn）                      │
│    聊天编排 + 记忆闭环 + 知识库 + 行为统计 + 反思生成        │
│    SQLite（WAL）+ sqlite-vec（cosine KNN）                  │
│    LLM：OpenCode Go（deepseek-v4-flash）· Embedding：智谱   │
└────────────────────────────────────────────────────┘
```

**一次聊天请求的数据流**：

```
用户消息 → 行为上下文 + 8 轮历史 + 记忆检索 + 知识库检索 + facts/教训/画像/术语/风格
        → 组装 system prompt → LLM 生成 → 写库（原文）
        → 每 4h：碎片提炼为 summary/topics/facts
        → 周日 20:00 画像 → 21:00 周报 → 22:00 每日小结 → 03:00 备份
```

## 记忆系统（八通道）

| 通道 | 表 | 注入时机 | 作用 |
|------|-----|----------|------|
| 情境记忆 | `memories` | 向量检索 Top-5 | 相似对话召回 |
| 持久事实 | `facts` | 每次必注入 | 身份/项目/偏好（"我叫小月"） |
| 教训 | `lessons` | 每次必注入 | 用户纠正永久遵守 |
| 画像 | `profile` | 每次必注入 | 四维用户理解 |
| 关切 | `concerns` | 每次必注入 | 在意的话题 + 搁置提醒 |
| 术语 | `jargon_terms` | 命中时注入 | 解释口径一致 |
| 风格范例 | `style_examples` | 每次必注入 | 认可的回复形式 |
| 行为上下文 | `behavior_events` | 每次必注入 | 当前窗口/提交/活跃 |

> 关键原则（LESSONS 6.6/6.7）：**确定事实走注入，不靠检索**——状态、身份、偏好类必须每次必达。

## 模块说明

| 目录 | 内容 | 技术栈 |
|------|------|--------|
| `server/` | 聊天编排、记忆闭环、RAG 知识库、行为统计、反思/备份定时任务、API 鉴权 | FastAPI · SQLite · sqlite-vec · APScheduler |
| `server/benchmarks/` | 检索评测（Hit@k/MRR，8 题测试集） | — |
| `server/scripts/` | 文档同步进知识库（docs/*.md → 可检索） | — |
| `collector/` | 三通道采集、隐私脱敏、断网落盘、心跳上报、Win32 API 封装 | Python · ctypes |
| `desktop/` | 自绘机器人（呼吸/眨眼/状态灯）、气泡面板（Markdown）、托盘、健康检查、开机自启 | PySide6 · Qt6 |
| `docs/` | 11 份文档（方案/参考/评审/提问/部署/踩坑/进度/运维/研究/测试 + 本文） | Markdown |
| `scripts/` | 服务器部署、开机自启、桌面打包 | bash · PowerShell |

## 快速开始

### 前置（一次性）

1. Windows 与服务器都安装 **Tailscale** 并登录同一账号（服务走加密专线，不暴露公网）
2. 服务器 `tailscale up`，Windows `tailscale set --unattended`（开机自动连）

### 1. 服务器端

```bash
git clone git@github.com:yh-user999/personal-ai-assistant.git
cd personal-ai-assistant/server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env   # 填 LLM/Embedding Key + 生成 API_TOKEN
setsid nohup .venv/bin/python run.py </dev/null > /tmp/assistant.log 2>&1 &
```

`.env` 要点：`API_TOKEN`（32 字节随机）、`LLM_BASE_URL`（OpenCode Go 或 DeepSeek 官方）、`EMBEDDING_DIMENSION`（智谱 embedding-3 = 2048）。

### 2. 采集器（Windows）

```powershell
git clone https://github.com/yh-user999/personal-ai-assistant.git
cd personal-ai-assistant\collector
python -m pip install -r requirements.txt
# .env: SERVER_URL=http://<服务器Tailscale IP>:8000 + API_TOKEN + GIT_REPOS
python main.py
# 开机自启（管理员 PowerShell）:  ..\scripts\install_autostart.ps1
```

### 3. 桌面机器人（Windows）

```powershell
cd ..\desktop
python -m pip install -r requirements.txt
python main.py   # 双击机器人开面板；📌 置顶；Esc/✕ 关闭
```

### 4. 同步项目文档进知识库（让机器人知道项目进展）

```bash
cd server && git pull
.venv/bin/python scripts/sync_docs_to_knowledge.py
```

## 测试与质量

| 项 | 状态 |
|----|------|
| 单元/集成测试 | **59 个**（`cd server && pytest tests/ -q`） |
| 检索评测 | `benchmarks/eval_retrieval.py`：基线 MRR 0.906 → 混合 0.938 |
| 踩坑沉淀 | 20+ 个真实问题，每个配"根因+修复+教训"（LESSONS.md） |
| 依赖 | requirements 全精确锁版 |

## 文档导航

| 文档 | 内容 |
|------|------|
| [实施方案细则](docs/实施方案细则.md) | 最初的设计蓝图与里程碑 |
| [REFERENCES](docs/REFERENCES.md) | 理论参考（认知科学/Agent 记忆范式）+ 学习路径 |
| [LESSONS](docs/LESSONS.md) | 实践日志：20+ 问题复盘 + 13 条工程原则 |
| [LEARNING_PROGRESS](docs/LEARNING_PROGRESS.md) | 六课+进阶课进度账本 |
| [OPS](docs/OPS.md) | 组件启停/日志/排查速查 |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | 部署环境（脱敏版） |
| [AI_REVIEW](docs/AI_REVIEW.md) | 三轮外部评审的采纳/拒绝 |
| [AI_OPTIMIZATION_PROMPTS](docs/AI_OPTIMIZATION_PROMPTS.md) | 让 AI 继续优化的提问模板 |
| [RESEARCH_GUIDE](docs/RESEARCH_GUIDE.md) | 心智功能术语地图 + 检索方法 |
| [TESTING_GUIDE](docs/TESTING_GUIDE.md) | 记忆/反思/人格/情绪测试法 |

## 路线图

| 优先级 | 内容 | 状态 |
|--------|------|------|
| 已完成 | 六课带教 + RAG 知识库（第 9 课）+ 检索评测（第 10 课） | ✅ |
| ⭐⭐⭐ | 第 11 课 Agent 工具链 / 第 12 课 Goal 系统 + unresolved 追踪 | ⏳ |
| ⭐⭐ | 第 6 课 CI / 第 7 课仪表盘 / 第 8 课 QQ 私聊接入 | ⏳ |

## 安全

- 全 API `API_TOKEN` 鉴权（32 字节随机，不入库）
- 服务仅 Tailscale 专线可达；公网仅暴露 SSH 22（密钥 + fail2ban）
- 事件出网前本地脱敏（密码/token/手机号/邮箱正则替换）
- 敏感信息（IP/密钥/token）只存本地笔记，**禁止入仓库**（每次提交前终扫）

## 许可证

私有项目，代码仅供本人使用。
