# AGENTS.md

本文件是本项目给新接入的模型 Agent 的快速上下文入口。开始任何代码、配置、部署或文档工作前先读本文件；完成阶段性工作后更新“当前状态”和“变更记录”。

## 1. 项目一句话

这是一个“服务器大脑 + 可选 Windows 客户端 + QQ/AstrBot 入口 + 小说工作台”的个人 AI 助手项目。服务器负责聊天、记忆、知识库、图片、提醒、任务和小说工作台；Windows 负责可选的本地采集、桌面端和远程执行；QQ 通过 NapCat + AstrBot 插件接入。

## 2. 代码地图

- `server/`：FastAPI 服务端、SQLite、聊天流水线、LLM/Embedding、记忆、知识库、提醒、小说工作台。
- `server/app/chat/`：聊天上下文、响应规划、检索、提示词、审校、群聊心流和续话。
- `server/app/chat/pipeline.py`：一轮聊天的主编排入口。
- `server/app/chat/retrieval.py`：记忆/知识库/群作用域检索和实时检索分流。
- `server/app/chat/prompting.py`：系统提示词与作用域边界；身份锚点是“小月”。
- `server/app/chat/response_plan.py`：响应模式、工具/联网规划和权限校验。
- `server/app/chat/followup.py`：服务端生成有限的群聊续话元数据。
- `server/app/chat/heartflow.py`、`attention_drift.py`：群聊拟人节奏和注意力表达规则。
- `qq/astrbot_plugin_xy/`：AstrBot QQ 插件；负责 QQ 事件门禁、群白名单、@/Reply/前缀、短时上下文窗口、HTTP 调用和发送回复。
- `docs/QQ_OPS.md`：QQ/NapCat/AstrBot 运维与安全边界。
- `docs/OPS.md`：服务端、Windows 客户端和组件排障；其中部分旧路径需以本文件的“已核对运行事实”为准。
- `spec://tasks.json`：当前任务队列；修改前必须先读，再就地编辑。

## 3. 已核对运行事实（2026-09-13）

### 服务器

- GitHub 仓库：`yh-user999/personal-ai-assistant`。
- 本次核对的实际部署仓库：`/opt/personal-ai-assistant`。
- FastAPI 实际运行目录：`/opt/personal-ai-assistant/server`，端口 8000；当前采用手工启动的 `run.py`，启动时显式固定端口 8000，避免继承宿主的其他端口环境。
- 服务日志：`/tmp/assistant.log`；检查优先使用 `/api/health` 和 `/api/ready`。
- 最近已部署功能提交：`7c934b1`（群聊通用上下文续话）。权威状态始终以 `git rev-parse HEAD` 和 `git ls-remote origin refs/heads/main` 为准，不要只相信本段文字。

### QQ / AstrBot（与服务器同机）

- AstrBot 工作目录：`/opt/astrbot`。
- systemd 服务：`astrbot.service`，工作目录 `/opt/astrbot`，用 `systemctl restart astrbot` 重载。
- AstrBot 实际插件副本：`/opt/astrbot/data/plugins/astrbot_plugin_xy`。
- AstrBot 配置：`/opt/astrbot/data/config/astrbot_plugin_xy_config.json`；该文件含敏感配置，禁止提交或在日志中打印原文。
- 当前已核对的安全门禁：`assistant_mode=group`、`group_require_mention=true`、`group_followup_enabled=true`、续话窗口 90 秒、每次最多 1 条、主动插话关闭。
- `group_allowed_ids` 当前是指定群白名单，不是 `*`；代码支持明确写 `*` 开放全部群，但实际配置变更必须单独审查并重载 AstrBot。
- QQ 插件更新不能只更新 `/opt/personal-ai-assistant/qq/`：必须从仓库同步后，把已审查的插件文件同步到 AstrBot 实际副本，再重启/重载 `astrbot.service`。

## 4. 当前功能边界

### 已完成

- 身份问题走确定性口径：群聊问“你是谁/你叫什么”等应直接回答“我是小月”。
- 群聊短时上下文续话：同一群、同一用户在小月成功直达回复后的短窗口内，可免 @ 续接一次；服务端显式澄清提示支持书名、链接、简介等补充请求。
- 群作用域不自动注入私聊画像；群级表达学习不应写入个人画像。
- `group_allowed_ids=*` 的配置解析和安全回归测试已实现，但实际运行配置尚未改为 `*`。
- 服务端与 QQ 插件均有回归测试、lint 和脱敏检查。

### 未完成/明确限制

- 群聊当前仍默认要求 @、前缀、真实回复或有效续话窗口；并没有实现 MaiBot 式“接收所有群消息后再统一决定是否回复”的完整聊天流。
- 群聊实时公网搜索当前被策略禁用：搜索后端本身可配置，但群检索路径不联网，只允许作用域内历史/记忆；要开放群联网，必须新增“公共来源、来源审校、群/用户限额、Token 熔断”的受控策略，不能只改一个开关。
- 最近一次运行日志显示上游 LLM 返回 HTTP 429（月度额度耗尽）；在额度恢复或切换可用模型前，不要把 QQ 手工验收失败归因于续话代码。
- 当前 Dynamic Spec 中仍有两个 blocked 方向：恢复 LLM 后复测身份/通用续话；配置 `group_allowed_ids=*` 后验证其他群。

## 5. 安全与隐私硬规则

- 绝不提交 `.env`、备份配置、真实 API Key、Bearer Token、密码、HMAC secret、真实 QQ 号、私有 IP 或其他个人敏感信息。
- 群聊请求始终使用 QQ 访客身份；不得把群消息当成主人身份，不得读取或注入私聊画像、主人事实或其他群数据。
- 修改服务器代码、配置、部署脚本或相关文档后：先做 staged 脱敏扫描，再提交并推送 GitHub；向用户报告提交号、远程同步状态和 Windows/部署端可执行的 GitHub 拉取命令。
- Windows 端更新来源统一为 GitHub：`git pull --ff-only origin main`；不要从服务器工作区直接复制到 Windows。
- 不要把实际配置文件复制进仓库；配置只在部署目录或 AstrBot 控制台维护。
- 不要使用破坏性 Git 操作，不要未经用户明确要求强推或修改历史。

## 6. 常用验证命令

在项目根目录：

```bash
git status --short --branch
git log -3 --oneline
```

服务端：

```bash
cd server
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check app tests
curl -s http://localhost:8000/api/health
curl -s http://localhost:8000/api/ready
```

QQ 插件：

```bash
cd qq
../server/.venv/bin/python -m pytest -q
../server/.venv/bin/ruff check astrbot_plugin_xy
```

检查 AstrBot 实际副本是否和仓库一致时，只比较哈希/文件内容摘要，不打印配置原文：

```bash
sha256sum /opt/personal-ai-assistant/qq/astrbot_plugin_xy/main.py \
  /opt/astrbot/data/plugins/astrbot_plugin_xy/main.py
systemctl show astrbot -p ActiveState -p SubState -p MainPID
```

## 7. 实时记录协议

每次阶段性工作完成后，在本文件末尾追加一条简短记录，并同步更新“已完成/未完成”段落。记录格式：

```text
### YYYY-MM-DD — <主题>
- 代码/配置：<改动摘要>
- 验证：<测试、lint、脱敏或部署结果>
- 提交：<commit 或“未提交”>
- 运行状态：<服务/插件是否重载>
- 未完成：<明确的 blocked/todo；没有就写“无”>
```

记录只写脱敏事实，不写 Token、密钥、QQ 号、私有 IP、完整日志或用户隐私。若本文件与代码/运行状态冲突，以代码、测试输出、`git` 状态和现场健康检查为准，并立即修正本文件。

## 8. 最近变更记录

### 2026-09-13 — 群聊续话与部署事实核对

- 代码/配置：群聊通用上下文续话、链接/简介/书名补充识别、身份确定性回答、全群白名单解析。
- 验证：服务端 1695 passed；QQ 插件 36 passed；两端 lint 和 staged 脱敏扫描通过。
- 提交：`7c934b1`；实际 FastAPI 仓库与 GitHub `origin/main` 已核对一致。
- 运行状态：FastAPI 已重启；AstrBot 实际插件副本已同步并重启 `astrbot.service`。
- 未完成：LLM 上游额度耗尽；群聊公共联网搜索未开放；`group_allowed_ids=*` 尚未应用；QQ 最终身份续话验收待额度恢复。

### 2026-09-13 — AGENTS.md 初始化

- 代码/配置：创建本文件，作为后续 Agent 的项目入口和实时状态记录。
- 验证：待本次提交前完成脱敏扫描。
- 提交：待提交。
- 运行状态：不改变运行服务。
- 未完成：无新增运行时改动。
