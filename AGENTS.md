# AGENTS.md

本文件是本项目给新接入的模型 Agent 的快速上下文入口。开始任何代码、配置、部署或文档工作前先读本文件；完成阶段性工作后更新“当前状态”和“变更记录”。

## 1. 项目一句话

这是一个“服务器大脑 + 可选 Windows 客户端 + QQ/MaiBot 入口 + 小说工作台”的个人 AI 助手项目。服务器负责聊天、记忆、知识库、图片、提醒、任务和小说工作台；Windows 负责可选的本地采集、桌面端和远程执行；当前 QQ 由 NapCat + MaiBot 接入，AstrBot 保留为旧链路回滚方案。

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

### 2.1 当前请求链路与职责归属

```text
QQ/NapCat 事件
  → AstrBot xy 插件：群白名单、真实 @/Reply/前缀、短时上下文、QQ 签名
  → /api/chat：认证与 ChatContext
  → routing：确定性快捷命令
  → pipeline：群心流/关系/插话闸门、响应规划、检索、生成、审校
  → ChatResponse：正文 + 有界 interaction 元数据
  → QQ 插件发送回复
```

当前职责边界：

- QQ 插件负责协议适配和第一道 fail-closed 门禁，不应继续承载完整的群聊人格判断。
- `server/app/chat/pipeline.py` 是群聊行为编排入口，负责把心流、主动插话、检索、生成和审校串起来。
- `server/app/chat/group_interjection.py` 负责非直达消息的确定性兴趣评分、冷却、小时上限和 reservation。
- `server/app/chat/heartflow.py` 负责群级活跃度、能量和连续回复限制；`group_relationship.py` 负责抽象互动熟悉度；`group_context.py` 负责短期话题上下文。
- `server/app/chat/followup.py` 与 QQ 插件的 `FollowupStore` 通过有限 `interaction` 字段协作，不保存原始消息或隐藏推理。
- `server/app/chat/review.py` 是候选回复的最终审校出口；没有可靠来源的群外部事实必须降级。

已知重复决策点：

- QQ 插件先判定是否直达，服务端随后再次判定 `group_directed`/`social_action`；未来应收敛为“插件做结构化入口，服务端做统一群聊决策”。
- `robot_state`、`heartflow`、`group_interjection` 都含活跃度/节奏信息；未来应由一个 GroupAgent 状态层统一读写。
- 追问状态在 QQ 插件内存和服务端 `interaction` 元数据之间分工；后续应保留插件的低成本门禁，把业务判定逐步移到服务端。
- 实际运行有两个必须同步的副本：Git 仓库 `/opt/personal-ai-assistant` 与 AstrBot 插件 `/opt/astrbot/data/plugins/astrbot_plugin_xy`。

建议的无行为变化收敛顺序：

1. 先把群聊决策输入统一成一个 `GroupDecision`，保留现有默认静默和白名单行为；
2. 再把心流、关系、插话闸门合并到单一状态接口；
3. 最后让插件只负责事件归一化和发送，服务端统一决定 `reply/wait/pass/interject`。

### 2.2 当前活动 QQ 链路（2026-09-13）

```text
QQ/NapCat 登录
  → NapCat 本机正向 OneBot WebSocket
  → MaiBot NapCat adapter
  → MaiBot core
```

- MaiBot 使用 host 网络连接 NapCat 本机正向 WebSocket；WebUI 仅绑定本机端口。
- 当前 MaiBot adapter 已切为群聊空黑名单，所有群消息可进入 adapter；是否实际回复仍由 MaiBot 核心聊天策略决定。
- `astrbot.service` 当前停止但未禁用；原 NapCat→AstrBot 反向 WebSocket 配置已备份，可按回滚步骤恢复。
- MaiBot 已配置可用的 Gemini provider/model；容器内最小 API 请求已通过，但 QQ 链路连接成功和 API 可用仍不等于身份回答和上下文续话验收通过。

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
- AstrBot 配置中的 `group_allowed_ids` 仍是指定群白名单；当前 QQ 由 MaiBot 接管，实际生效的群范围由 MaiBot NapCat adapter 的群聊空黑名单控制。
- QQ 插件更新不能只更新 `/opt/personal-ai-assistant/qq/`：必须从仓库同步后，把已审查的插件文件同步到 AstrBot 实际副本，再重启/重载 `astrbot.service`。
- 当前 QQ 已切换到 `/opt/maibot` 的 MaiBot 核心：NapCat 本机正向 WebSocket → MaiBot NapCat adapter；`astrbot.service` 停止但未禁用。
- MaiBot WebUI 当前实际监听本机 8001；NapCat 正向 WebSocket 使用本机 3001；两者均未暴露公网。

## 4. 当前功能边界

### 已完成

- 身份问题走确定性口径：群聊问“你是谁/你叫什么”等应直接回答“我是小月”。
- 群聊短时上下文续话：同一群、同一用户在小月成功直达回复后的短窗口内，可免 @ 续接一次；服务端显式澄清提示支持书名、链接、简介等补充请求。
- 群作用域不自动注入私聊画像；群级表达学习不应写入个人画像。
- `group_allowed_ids=*` 的配置解析和安全回归测试已实现，但实际运行配置尚未改为 `*`。
- 服务端与 QQ 插件均有回归测试、lint 和脱敏检查。
- MaiBot 核心已旁路部署到 `/opt/maibot`，仅本机 WebUI 可访问。
- 当前 QQ 已由 MaiBot NapCat adapter 接管，AstrBot 保留为停止状态的回滚链路。
- MaiBot WebUI 已保存 Gemini provider 与 `gemini-3.8-flash-high` 模型；容器内 `/v1/models` 与最小聊天请求均验证通过。

### 未完成/明确限制

- MaiBot adapter 已可接收非黑名单群消息，但核心回复策略仍可能因频率门、Focus/wait 状态、空闲退避或模型选择 wait/no_action 而不发言；当前没有“每条消息必回”保证。
- “非 @ 不插话”尚未应用：当前实现中把 `talk_value` 设为 0 会先进入静默消费分支，并连带跳过 @ 强制触发，不能直接作为“只有 @ 才回”的配置。
- 群聊实时公网搜索当前被策略禁用：搜索后端本身可配置，但群检索路径不联网，只允许作用域内历史/记忆；要开放群联网，必须新增“公共来源、来源审校、群/用户限额、Token 熔断”的受控策略，不能只改一个开关。
- 原 AstrBot/FastAPI 链路曾出现上游 LLM HTTP 429（月度额度耗尽）；当前 MaiBot Gemini 最小请求已通过，但仍不把 QQ 手工验收提前视为通过。
- 当前 Dynamic Spec 中仍有 QQ 身份/通用上下文续话复测，以及其他群真实消息回复验收两个方向。
- MaiBot 当前已连接 NapCat 并接管 QQ，Gemini provider 已可用，群聊空黑名单已加载；不得把适配器连接、群范围配置或 API 直测成功误认为身份回答、通用续话或其他群实际回复验收通过。

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

### 2026-09-13 — MaiBot 接管现有 QQ 登录

- 代码/配置：通过 GitHub 发布无密钥 QQ 切换脚本；备份 NapCat/AstrBot/MaiBot 配置，关闭 AstrBot 路由，启用 NapCat 本机正向 WebSocket，并让 MaiBot host 网络适配器接管当前 QQ。
- 验证：MaiBot NapCat adapter 已连接；NapCat 正向 WebSocket 已监听；现有群白名单保留；MaiBot WebUI GET 返回 200；回滚备份存在；AstrBot 停止但未禁用。
- 提交：切换脚本 `d0267b1`，BOM 兼容修复 `5f7973b`；本次运行记录待随文档提交。
- 运行状态：QQ 当前由 MaiBot 处理，AstrBot 作为回滚链路停止。
- 未完成：LLM 额度/可用模型仍阻塞 QQ 身份回答与通用续话验收；尚未开放全部群。

### 2026-09-13 — MaiBot 旁路核心部署

- 代码/配置：新增无密钥 MaiBot 核心启动脚本；从 GitHub 同步到实际部署仓库，在 `/opt/maibot` 创建空 MaiBot 持久化目录并启动核心容器；未修改 AstrBot、NapCat 或现网 QQ 路由。
- 验证：MaiBot WebUI GET 返回 200；容器持续运行、重启策略为 `unless-stopped`；AstrBot 为 active/running，NapCat 容器正常。
- 提交：部署脚本 `665532d`；本次运行记录待随文档提交。
- 运行状态：MaiBot 核心已运行，QQ 仍由 AstrBot 处理。
- 未完成：MaiBot 尚未配置可用 LLM、连接 NapCat 或进行 QQ 验收；当前 LLM 配额阻塞仍保留。

### 2026-09-13 — 架构链路盘点

- 代码/配置：核对 QQ/AstrBot/FastAPI 实际运行副本，梳理群聊门禁、服务端社交闸门、心流/关系状态、检索、生成和审校职责；记录重复决策点与后续收敛顺序。
- 验证：仓库与实际 AstrBot 插件文件哈希已核对；FastAPI/AstrBot 运行状态已现场确认。
- 提交：待提交。
- 运行状态：不改变运行服务。
- 未完成：尚未进行行为不变的模块重构。

### 2026-09-13 — AGENTS.md 初始化

- 代码/配置：创建本文件，作为后续 Agent 的项目入口和实时状态记录。
- 验证：待本次提交前完成脱敏扫描。
- 提交：待提交。
- 运行状态：不改变运行服务。
- 未完成：无新增运行时改动。

### 2026-09-13 — MaiBot Gemini provider 验证

- 代码/配置：核对 WebUI 保存的 Gemini provider 与 `gemini-3.8-flash-high`；补齐 MaiBot 标准 Bearer 鉴权字段，未写入或提交任何密钥。
- 验证：容器内模型列表 HTTP 200（17 个模型）；指定模型最小聊天请求 HTTP 200；MaiBot 重启后初始化完成、NapCat 适配器与元事件重新连接；WebUI HTTP 200。
- 提交：未提交；运行时配置仅保留在 `/opt/maibot`，不进入 Git。
- 运行状态：MaiBot 持续运行并接管 QQ；AstrBot 保持停止但未禁用；NapCat 正常。
- 未完成：QQ 身份回答与通用上下文续话仍待实际消息验收；全部群开放未启用。

### 2026-09-14 — MaiBot 全群接收配置脚本

- 代码/配置：新增无密钥配置脚本，将 MaiBot 群聊名单切换为空黑名单，同时保留私聊白名单和总过滤开关；新增脱敏 fixture 测试。
- 验证：脚本测试 4 passed；ruff 通过；Python 语法检查通过；现网配置仍为群聊白名单，尚未写入运行时。
- 提交：`47f83e8`；已推送 GitHub，实际部署仓库已 fast-forward 同步。
- 运行状态：已备份并应用 MaiBot 群聊空黑名单；重载后容器运行正常、WebUI 返回 200、NapCat adapter 重新连接；AstrBot 保持停止但未禁用。
- 未完成：其他群真实消息回复验收待完成；QQ 身份回答与通用上下文续话仍待实际消息验收。

### 2026-09-14 — MaiBot 全群接收配置应用

- 代码/配置：从 GitHub 同步 `47f83e8`；备份 MaiBot adapter 运行时配置，将群聊名单切换为空黑名单，保持总过滤开关和私聊白名单不变。
- 验证：运行时摘要为过滤开启、群模式 blacklist、群列表 0、私聊 whitelist；备份权限 600；MaiBot 容器稳定运行，WebUI HTTP 200，NapCat adapter 重新连接。
- 提交：本次状态记录待提交；运行时配置仅保留在 `/opt/maibot`，不进入 Git。
- 运行状态：MaiBot 已重载并继续接管 QQ；AstrBot 保持停止但未禁用；NapCat 正常。
- 未完成：尚未收到其他群真实消息回复证据；QQ 身份回答与通用上下文续话仍待实际消息验收。

### 2026-09-14 — MaiBot 未回复原因与非 @ 策略诊断

- 代码/配置：未修改 MaiBot 运行时配置；核对当前回复时序字段为群聊频率 1、私聊频率 1、频率触发、@ 必回复开启、文字提及不强制回复、动态频率规则关闭。
- 验证：读取容器内调度器和频率门逻辑；普通群消息当前通常可进入 Planner，但 Focus/wait 状态、空闲退避、频率门，以及模型选择 wait/no_action 都可能导致不发言；频率为 0 时会先进入静默消费并跳过 @ 强制触发。
- 提交：`6bb6490`；已完成 staged 脱敏扫描、推送 GitHub，并同步部署仓库。
- 运行状态：未重载，未改变服务。
- 未完成：尚未应用“仅 @ 回复”方案；真实 QQ 回复、身份回答和通用续话验收仍按用户暂缓保持 blocked。
