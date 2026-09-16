# AGENTS.md

本文件是本项目给新接入的模型 Agent 的快速上下文入口。开始任何代码、配置、部署或文档工作前先读本文件；完成阶段性工作后更新“当前状态”和“变更记录”。

## 1. 项目一句话

这是一个“服务器大脑 + 可选 Windows 客户端 + QQ 入口 + 小说工作台”的个人 AI 助手项目。服务器负责聊天、记忆、知识库、图片、提醒、任务和小说工作台；Windows 负责可选的本地采集、桌面端和远程执行。QQ 侧保留 NapCat 登录与 OneBot 能力，并由仓库内独立的 `qq/onebot_gateway` 薄网关承接入站；MaiBot 与 AstrBot 两套第三方机器人链路已于 2026-09-15 彻底删除。

## 2. 代码地图

- `server/`：FastAPI 服务端、SQLite、聊天流水线、LLM/Embedding、记忆、知识库、提醒、小说工作台。
- `server/app/chat/`：聊天上下文、响应规划、检索、提示词、审校和零 LLM 快捷命令路由。
- `server/app/chat/pipeline.py`：一轮聊天的编排骨架（私聊全链路 + 群聊分派）。
- `server/app/chat/retrieval.py`：记忆/知识库/群作用域检索和实时检索分流。
- `server/app/chat/prompting.py`：系统提示词与作用域边界；身份锚点是“小月”。
- `server/app/chat/response_plan.py`：响应模式、工具/联网规划和权限校验。
- `server/app/chat/followup.py`：服务端生成有限的群聊续话元数据。
- `server/app/fitness/`：健身领域包（事实记录、动作目录、训练计划、营养、教练、`api.py`）。
- `server/app/group/`：群聊领域包（`context`/`heartflow`/`interjection`/`attention`/`care`/`expression`/`profile`/`relationship`，`turn.py` 承载群聊轮次编排）；可达性见 `server/app/group/README.md`。
- `server/app/novel/`：小说领域包（实体、词典、写作、章节分析、生成、审阅和 `api.py`）。
- `server/benchmarks/`：离线评分/回放基准工具（如 `social_replay.py` 群聊社交回放）。
- `qq/onebot_gateway/`：独立 OneBot v11 HTTP 网关，负责事件解析、群白名单、真实 At/Reply/前缀门禁、follow-up、`group_directed` 映射和 NapCat 回复。
- `docs/QQ_MENTION_REFERENCE.md`：历史 @ 判定语义摘录（fail-closed 判据、组件字段兼容坑、限流取舍），网关实现与测试的参考。
- `docs/QQ_OPS.md`：QQ/NapCat 运维与安全边界；其中 AstrBot 相关章节已失效，需以本文件的“已核对运行事实”为准。
- `docs/OPS.md`：服务端、Windows 客户端和组件排障；服务端管理方式（systemd 与日志位置）已于 2026-09-15 更新，如与现场冲突以本文件的“已核对运行事实”为准。
- `docs/模块开发指南.md`：目录归属、四件套契约（service/api/建表迁移/聊天触发词/测试）与新增模块注册流程；新增领域模块前先读。
- `spec://tasks.json`：当前任务队列；修改前必须先读，再就地编辑。

### 2.1 当前请求链路

```text
QQ/NapCat 登录（保留）
  → OneBot reverse HTTP 事件
  → qq.onebot_gateway（唯一群直达决策层）
  → FastAPI /api/chat（身份、作用域、编排和最终安全约束）
  → OneBot HTTP action 回 NapCat
```

网关已在仓库实现，但尚未应用到部署机或 NapCat 运行配置：

- `qq/onebot_gateway/event.py` 只接受可确认的 OneBot 文本、真实 At 和 Reply 目标；无法确认时 fail-closed。
- `qq/onebot_gateway/gate.py` 负责群白名单、`group_directed`、成功回复限流和有限 follow-up。
- `qq/onebot_gateway/chat.py` 复用服务端 QQ 身份 HMAC、访客 Bearer 和 `request_id` 幂等契约。
- `qq/onebot_gateway/app.py` 编排入站事件、FastAPI 调用与非空回复发送；媒体等非文本事件安全忽略。
- `server/app/chat/pipeline.py` 是聊天编排骨架；群聊行为编排在 `server/app/group/turn.py`，服务端继续负责最终审校。
- `server/app/chat/followup.py` 生成有限的 `interaction` 元数据，由网关 `FollowupStore` 消费。

### 2.2 新架构决策结果（2026-09-16）

1. QQ 接入采用自建、可由 systemd 托管的薄 OneBot v11 HTTP 网关，不重新引入 AstrBot/MaiBot/NoneBot。
2. 网关是群消息是否直达的唯一确定性决策层，并把结果映射为服务端现有的 `group_directed`；服务端保留最终权限、安全和响应审校。
3. 真实 At、Reply 目标确认、配置前缀和有限 follow-up 才能放行群直达；无法确认账号或引用目标时拒绝触发。非直达默认不调用服务端，主动插话必须显式开启。
4. 网关复用服务端的 QQ 身份 HMAC、访客 token、群作用域、`request_id` 幂等和 OneBot action 发送出口。

## 2.3 服务端只读架构审计结论（2026-09-16）

- FastAPI 入口由 `server/run.py` 启动，`server/app/main.py` 负责生命周期、鉴权中间件、路由注册、健康检查和静态页；另有 MCP 入口，OneBot 入站由独立 `qq/onebot_gateway` 进程承接。
- HTTP 聊天主链路为 `api.chat` 兼容层 → `services_registry` 全量模块装配 → `chat.pipeline` → 群聊闸门/响应规划 → 检索 → 提示词 → LLM → 审校 → 持久化；MCP 与部分领域 API 仍绕过统一应用服务。
- `services/` 是混合共享层，不是稳定边界；`chat` 同时承载路由、领域触发、检索、提示词、审校和基础设施编排；fitness/novel/group 虽已成包，仍直接依赖 SQLite、core 和旧 services。
- 已确认循环/反向依赖：`models.database` 的迁移逻辑延迟导入 `services.self_reflect`；`chat.retrieval` 反向导入 `chat.prompting`；`api.chat ↔ chat.routing`、`models.repo → core.memory`、`core ↔ services` 等靠延迟导入或兼容层维持。
- 模块化单体目标分层为 `transport → application → domain → ports → adapters`；保留单进程、单 SQLite 和现有领域代码，先用兼容包装迁移，不拆微服务。
- 迁移顺序：冻结 HTTP/MCP/聊天契约并补回归基线 → 建依赖约束 → 抽取 identity/scope 与基础端口 → 收敛 ChatApplication → 统一 fitness/novel/group 应用门面 → 接入薄 OneBot 网关 → 清理兼容层。

## 3. 已核对运行事实（2026-09-15）

### 服务器

- GitHub 仓库：`yh-user999/personal-ai-assistant`。
- 本次核对的实际部署仓库：`/opt/personal-ai-assistant`。
- FastAPI 实际运行目录：`/opt/personal-ai-assistant/server`，端口 8000；当前由 systemd 单元 `personal-assistant.service` 管理（`User=paa`、`EnvironmentFile` 提供 `PORT=8000`、enabled 开机自启），手工 `setsid nohup` 方式已停用。
- 服务日志：`journalctl -u personal-assistant`（旧 `/tmp/assistant.log` 已停止写入）；检查优先使用 `/api/health` 和 `/api/ready`。
- 部署仓库同步：`paa` 直接对 `/opt/personal-ai-assistant` 执行 `git pull --ff-only`（SSH 凭据在该仓库 `.ssh/`，权限 600、直连 GitHub、不进入 Git）；命令见第 6 节。
- 最近已部署功能提交：`7c934b1`（群聊通用上下文续话）。权威状态始终以 `git rev-parse HEAD` 和 `git ls-remote origin refs/heads/main` 为准，不要只相信本段文字。

### QQ / NapCat（与服务器同机，2026-09-15 核对）

- NapCat 容器：`napcat`，`network=host`，`restart=always`；持久化目录 `/opt/napcat/{qq_config,cache,config,qq-login}`。
- QQ 登录态保留在 `/opt/napcat/qq_config`，清理过程中未重新扫码。
- NapCat OneBot 配置中当前只保留 `httpServers[xy-push]`；原 `websocketServers[maibot]` 与 `wsReverse/websocketClients[astrbot]` 条目已删除。
- 仓库新增 `scripts/personal-qq-gateway.service` 模板，默认网关监听 `127.0.0.1:3101`；本阶段未应用部署机 systemd 或 NapCat reverse HTTP 配置。
- 本机端口现状：`6099`（NapCat WebUI）开放；`3001`（原 MaiBot 正向 WebSocket）已随配置删除而关闭；`3101` 尚未作为现网服务启用。
- MaiBot 与 AstrBot 已彻底删除：容器 `maim-bot-core`、镜像 `sengokucola/maibot:latest`、目录 `/opt/maibot`、`astrbot.service` 单元与 `/opt/astrbot` 均不存在。
- 清理前的配置类文件已转存到服务器本地 `0700` 备份目录 `/opt/cleanup-backup-<UTC 时间戳>/`，仅保留在服务器，不进入 Git。
- 2026-09-15 二次清理：4 条失效 crontab 任务（指向已删脚本与 `/opt/astrbot`）、26 个挂死部署进程与 `/opt/astrbot`、`/opt/health-dash`、`/opt/astrbot_plugin_meme_manager`、`/opt/meme.tar.gz` 等残留已处置；保活/自愈任务 `jd-qqwatch`、`jd-shield` 与备份任务 `jd-backup` 保留。
- 当前运行中的容器只有 `napcat` 与 `searxng`。

## 4. 当前功能边界

### 已完成

- 身份问题走确定性口径：群聊问“你是谁/你叫什么”等应直接回答“我是小月”。
- 群聊短时上下文续话：同一群、同一用户在小月成功直达回复后的短窗口内，可免 @ 续接一次；服务端显式澄清提示支持书名、链接、简介等补充请求。
- 群作用域不自动注入私聊画像；群级表达学习不应写入个人画像。
- 服务端与历史 QQ 插件源码均有回归测试、lint 和脱敏检查。
- MaiBot 与 AstrBot 两套第三方机器人链路已彻底清理，QQ 登录态保留在 NapCat。
- 代码结构重组完成：健身/群聊/小说各自成领域包（`app/fitness`、`app/group`、`app/novel`），`app/chat/pipeline.py` 只留编排骨架，群聊轮次编排在 `app/group/turn.py`；新增模块流程见 `docs/模块开发指南.md`。
- 服务端只读架构审计、模块化单体分阶段方案、契约回归基线、依赖方向检查、identity/scope 基础抽取、ChatApplication HTTP 入口收敛和 MCP 记忆/知识工具接入 application ports 已完成。
- QQ 薄网关已实现：OneBot v11 HTTP 事件入口、真实 At/Reply/前缀 fail-closed 门禁、`group_directed` 映射、短时 follow-up、发送限流、HMAC/幂等客户端、systemd 模板和契约测试已加入仓库。

### 未完成/明确限制

- QQ 入口代码已具备，但部署机尚未安装/启用 `personal-qq-gateway.service`，NapCat reverse HTTP 运行配置也尚未修改；因此真实 QQ 消息回复仍未验收。
- 网关当前只处理文本事件；图片、语音、视频和文件安全忽略，媒体入口另行设计。
- 服务端已由 systemd 管理（`EnvironmentFile` 固定 `PORT=8000`），不再受宿主 `PORT` 继承问题影响；仅手工调试时才需要 `env -u PORT PORT=8000 .venv/bin/python run.py`。
- 群聊实时公网搜索当前被策略禁用：搜索后端本身可配置，但群检索路径不联网，只允许作用域内历史/记忆；要开放群联网，必须新增“公共来源、来源审校、群/用户限额、Token 熔断”的受控策略，不能只改一个开关。
- 此前上游 LLM 曾出现 HTTP 429（月度额度耗尽）；新链路接入后仍需重新确认可用额度。
- 群聊真实消息回复、身份回答与通用续话仍未在部署机验收；网关代码测试已通过，但运行环境尚未接线。
- `qq/`、`deploy/maibot/` 与根 `data/` 已于 2026-09-15 从仓库删除；@ 判定语义见 `docs/QQ_MENTION_REFERENCE.md`，原实现可在 Git 历史中查阅。
- 模块化单体尚未完成 fitness/novel/group application 门面迁移。

## 5. 安全与隐私硬规则

- 绝不提交 `.env`、备份配置、真实 API Key、Bearer Token、密码、HMAC secret、真实 QQ 号、私有 IP 或其他个人敏感信息。
- 群聊请求始终使用 QQ 访客身份；不得把群消息当成主人身份，不得读取或注入私聊画像、主人事实或其他群数据。
- 修改服务器代码、配置、部署脚本或相关文档后：先做 staged 脱敏扫描，再提交并推送 GitHub；向用户报告提交号、远程同步状态和 Windows/部署端可执行的 GitHub 拉取命令。
- Windows 端更新来源统一为 GitHub：`git pull --ff-only origin main`；不要从服务器工作区直接复制到 Windows。
- 不要把实际配置文件复制进仓库；配置只在部署目录或对应组件控制台维护。清理备份目录 `/opt/cleanup-backup-*` 同样禁止进入 Git。
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

服务端由 systemd 管理（端口由单元 `EnvironmentFile` 固定为 8000）：

```bash
systemctl status personal-assistant --no-pager
systemctl restart personal-assistant
journalctl -u personal-assistant -n 50 --no-pager
```

检查 QQ 侧运行状态（当前只剩 NapCat）：

```bash
docker ps -a --format '{{.Names}}|{{.Status}}'
docker inspect napcat --format 'status={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}'
```

同步部署仓库到最新提交（以 `paa` 直连拉取；仅当更新包含 `server/` 代码时才重启服务）：

```bash
sudo -u paa -H git -C /opt/personal-ai-assistant pull --ff-only origin main
systemctl restart personal-assistant   # 仅 server/ 代码有更新时需要
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

### 2026-09-15 — 清理 MaiBot 与 AstrBot 机器人链路

- 代码/配置：先把 AstrBot/MaiBot/NapCat 配置类文件转存到服务器本地 `0700` 备份目录（34 个文件，不进入 Git）；删除 NapCat 中 `websocketServers[maibot]`、`wsReverse/websocketClients[astrbot]` 条目，保留 `httpServers[xy-push]`；更新本文件的链路、运行事实、功能边界与验证命令。
- 验证：NapCat 重启后 `running=true`、登录态目录完整、未出现二维码或掉线；本机 `6099` 开放、`3001` 已关闭；容器仅剩 `napcat` 与 `searxng`；`astrbot*` systemd 单元数为 0；`/opt/maibot` 与 `/opt/astrbot` 均已不存在；根分区占用由 32G 降至 28G。
- 提交：文档改动待本次脱敏扫描后提交。
- 运行状态：删除容器 `maim-bot-core`、镜像 `sengokucola/maibot:latest`、目录 `/opt/maibot`；`systemctl disable --now astrbot` 并删除单元与 `/opt/astrbot`；删除从未启动的空容器 `naughty_gould`；NapCat 与 searxng 保持运行；FastAPI 本轮未启动。
- 未完成：QQ 入口当前无下游消费者，群聊不会回复；新 QQ 接入架构与“仅真实 @ 回复”门禁落点待决策。

### 2026-09-15 — 仓库清理为重构腾空间

- 代码/配置：删除 `qq/astrbot_plugin_xy/`（AstrBot 插件）、`deploy/maibot/`（MaiBot 部署脚本）及其专属测试 `server/tests/test_qq_{file_ingest,plugin_gate}.py`；删除根 `data/`（87 个 AstrBot 运行时残留，其中 83 个 md 均为 19 字节占位、非本项目小说数据）；删除损坏库副本 `assistant-corrupted-20260901.db` 与其 WAL/SHM；`server/data/backups/` 保留最新 weekly、删除 3 个过期备份；清理 23 个缓存目录、12 个临时文件（含 `.env.bak`、空 `nohup.log`）；新增 `docs/QQ_MENTION_REFERENCE.md` 摘录 fail-closed @ 判定语义。
- 验证：删除前后核对 `knowledge_chunks`=3617、`memories`=955、`novel_projects`=7、`novel_entities`=160、`reminders`=7、`profile`=4、`fitness_facts`=21 全部一致，`integrity=ok`；服务端 1672 passed（较基线少 23 项即随插件删除的测试）、`ruff` 全通过；FastAPI 重启后 `/api/health` 与 `/api/ready` 均 200，database/scheduler/llm/vector 全 ok。
- 提交：本次改动待脱敏扫描后提交。
- 运行状态：FastAPI 已重启并运行在 8000；NapCat 与 searxng 未受影响；`.env` 与 `server/.venv/` 保持完整。
- 未完成：QQ 入口仍无下游消费者；新接入架构待决策，群聊真实消息验收继续 blocked。

### 2026-09-15 — 清理后服务端健康基线

- 代码/配置：未改动业务代码；在本文件记录 `run.py` 会继承宿主 `PORT` 的启动陷阱和显式固定端口的启动命令。
- 验证：服务端 1695 passed、`ruff` 全通过；历史 QQ 插件源码 36 passed、`ruff` 全通过；FastAPI 以 `PORT=8000` 启动后 `/api/health` 与 `/api/ready` 均 200，`ready` 中 database（schema 17、integrity 正常）、scheduler、llm、vector 全部 ok，无 failures/degraded。
- 提交：本次文档改动待脱敏扫描后提交。
- 运行状态：FastAPI 已在 8000 端口运行；NapCat 与 searxng 保持运行；未新增任何 QQ 侧组件。
- 未完成：QQ 入口仍无下游消费者；新架构三项待决问题未定，群聊真实消息验收继续 blocked。

### 2026-09-15 — 服务端代码结构重组（领域包 + pipeline 瘦身）

- 代码/配置：新建 `server/app/fitness/`（7 个文件）与 `server/app/group/`（含 `turn.py`、`README.md`）领域包，迁入原 `app/api`、`app/services`、`app/chat` 中健身/群聊/小说专属模块并修正全部 import 与 monkeypatch 目标；`social_replay.py` 等基准工具移入 `server/benchmarks/`；`server/app/chat/pipeline.py` 从 1115 行降到 783 行，群聊轮次编排（前置场景、计划字段、提示注入、收尾记账）落到 `server/app/group/turn.py`；新增 `docs/模块开发指南.md`（目录归属、四件套契约、注册流程、待建模块清单）；README 目录结构与本文件代码地图同步。
- 验证：服务端 1672 passed（与基线一致）、`ruff check app tests` 全通过；工作区数据库基线未变（`knowledge_chunks`=3617、`memories`=955、`novel_projects`=7、`novel_entities`=160、`fitness_facts`=21、`reminders`=7、schema=17、integrity=ok）；staged 脱敏扫描 0 命中。部署库 `/opt/personal-ai-assistant/server/data/assistant.db` 是长期独立演进的历史数据集（与工作区副本计数不同属既有事实），本轮未改动两者内容。
- 提交：`d71cc0f`；已推送 GitHub `origin/main`，部署仓库 `/opt/personal-ai-assistant` 已 fast-forward 同步。
- 运行状态：FastAPI 已按显式端口方式从部署目录重启于 8000；`/api/health` 200，`/api/ready` 就绪（database schema 17、integrity ok，scheduler/llm/vector 全 ok，无 failures/degraded）。
- 未完成：QQ 入口仍无下游消费者；新接入架构三项待决问题未定，群聊真实消息验收继续 blocked。

### 2026-09-15 — 环境残留清理与服务 systemd 化

- 代码/配置：终止 26 个自 2026-08-26 起挂死的部署/更新进程（`git fetch`/`git pull` 卡死链，个别脚本的后续命令含会误杀服务进程的 `pkill`）；清理 root crontab 中 4 条指向已删脚本与 `/opt/astrbot` 的失效任务（保留 `jd-shield`、`jd-qqwatch`、`jd-backup`）；改造 `/usr/local/bin/jd-backup.sh`（去掉已删路径引用，备份前缀改为 `napcat-` 并纳入 7 份轮换）；将 AstrBot 保活报告等敏感残留与 `/opt/astrbot`、`/opt/health-dash`、`/opt/astrbot_plugin_meme_manager`、`/opt/meme.tar.gz` 转存 `0700` 备份后清除；服务端从手工进程切换为 systemd 单元 `personal-assistant.service`（`User=paa`、`EnvironmentFile` 固定端口 8000、enabled）；同步修正本文件与 `docs/OPS.md`、`docs/DEPLOYMENT.md`、`docs/QQ_OPS.md`、`pyproject.toml` 中的失效注释。
- 验证：服务端 1672 passed、`ruff` 全通过；crontab 差异恰为 4 行删除；`jd-backup.sh` 语法检查与实跑通过（新备份 3.7K）；systemd 启动与 restart 后 `/api/health`、`/api/ready` 全绿，进程以 `paa` 运行、journal 无错误；staged 脱敏扫描 0 命中。
- 提交：`a38db20`；已推送 GitHub `origin/main`，部署仓库已 fast-forward 同步。
- 运行状态：FastAPI 由 systemd 托管于 8000；NapCat 与 searxng 未受影响；手工 `setsid nohup` 启动方式停用，`/tmp/assistant.log` 停止写入。
- 未完成：QQ 入口仍无下游消费者；新接入架构待决策；`/var/backups/` 中 6 份含已删 AstrBot 数据的旧备份（约 1.2G）是否清理待用户决定。

### 2026-09-15 — 部署仓库同步通道与清理收尾

- 代码/配置：为部署用户 `paa` 配置仅限本机的 GitHub SSH 通道（`/opt/personal-ai-assistant/.ssh/`：SSH 私钥、known_hosts、直连配置，600/700；不使用 ProxyCommand——`sudo -u paa -H` 下 `SHELL=/usr/sbin/nologin` 会让代理命令无法执行）；`.gitignore` 新增 `.ssh/` 防护；修正部署仓库 `.git` 内 1089 个历史 root 属主对象为 `paa`；部署仓库残留（已删 `qq/` 空壳、4 份 `.env.bak-*`）转存 `/opt/cleanup-backup-20260915T090059Z/paa-env-baks/`（0700），不进入 Git；处置 `/var/backups` 中 6 份 AstrBot 旧备份（用户确认保留最新 9/15 快照 202M，清理 9/10–9/14 五份约 1G；删除前留存清单，全部加固 600）。
- 验证：`paa` 执行 `git ls-remote` 与 `git pull --ff-only` 成功（`36c2c50`→`a38db20`，fast-forward，5 文件）；部署仓库 HEAD 与 `origin/main` 一致、拉取文件属主为 `paa`、`git status` 干净且 `.ssh/` 被忽略；保留份备份 gzip 完整性校验通过；`/api/health` 返回 ok；staged 脱敏扫描 0 命中。
- 提交：`a38db20`（主改动）、`c7d6162`（同步通道与收尾记录）；备份处置随本次补充提交。
- 运行状态：systemd 服务保持运行，本轮无业务代码变更、无需重载。
- 未完成：QQ 入口仍无下游消费者；新接入架构待决策。

### 2026-09-16 — 服务端只读架构审计与重构分阶段方案

- 代码/配置：未改业务代码；完成入口、调用链、模块边界、循环/反向依赖、测试契约和 QQ 残留契约审计；确定模块化单体目标分层与六阶段迁移顺序。
- 验证：静态阅读、依赖图扫描和独立交叉审计完成；本轮无代码改动，未运行全量测试或服务重载。
- 提交：未提交。
- 运行状态：systemd 服务、NapCat 与 searxng 均未改变。
- 未完成：契约回归基线和依赖约束检查进入下一阶段；QQ 入站方案、`group_directed` 与真实 @ 门禁归属仍待决定。

### 2026-09-16 — 契约回归基线

- 代码/配置：新增 `server/tests/test_contract_baseline.py`，锁定关键 HTTP 路由/方法、鉴权规则覆盖、聊天请求/响应字段、群聊 `interaction`、MCP 工具唯一性与关闭语义；未改生产运行时代码。
- 验证：相关回归 50 passed；服务端全量 1678 passed；`ruff check server/app server/tests` 通过；脱敏扫描（Token/邮箱/IPv4/QQ 号）无命中。
- 提交：未提交。
- 运行状态：systemd 服务、NapCat 与 searxng 未改变，未重启服务。
- 未完成：身份作用域与基础端口进入下一阶段；QQ 入站方案、`group_directed` 与真实 @ 门禁归属仍待决定。

### 2026-09-16 — 模块化单体依赖方向检查

- 代码/配置：新增 `server/tests/test_architecture_boundaries.py`，以 AST 检查目标层依赖方向、domain 禁止传输/存储 SDK，并逐条记录当前 legacy 反向依赖豁免；未改生产运行时代码。
- 验证：依赖边界定向测试 3 passed；服务端全量 1681 passed；`ruff check server/app server/tests` 通过；脱敏扫描（Token/邮箱/IPv4/QQ 号）无命中。
- 提交：未提交。
- 运行状态：systemd 服务、NapCat 与 searxng 未改变，未重启服务。
- 未完成：ChatApplication 收敛进入下一阶段；QQ 入站方案、`group_directed` 与真实 @ 门禁归属仍待决定。

### 2026-09-16 — identity/scope 与基础端口抽取

- 代码/配置：新增纯 `domain.identity`、配置适配层 `app.identity` 与 `contracts` 中的 Memory/Knowledge/LLM Protocol；`core.memory` 保留兼容导出，`models.repo` 改为直接依赖 identity，ChatRuntime 改用端口类型注解；未改数据库 schema 或 HTTP/MCP 运行时行为。
- 验证：身份边界定向测试 44 passed；服务端全量 1686 passed；`ruff check server/app server/tests` 通过；脱敏扫描（Token/邮箱/IPv4/QQ 号）无命中。
- 提交：未提交。
- 运行状态：systemd 服务、NapCat 与 searxng 未改变，未重启服务。
- 未完成：ChatApplication 收敛进入下一阶段；QQ 入站方案、`group_directed` 与真实 @ 门禁归属仍待决定。

### 2026-09-16 — ChatApplication HTTP 入口收敛

- 代码/配置：新增 `app/application/chat.py` 与 `app/chat/composition.py`，将聊天 runtime、主/流式用例移入 ChatApplication；HTTP API 保留幂等、鉴权、上传和 SSE 适配，routing 不再反向导入 `app.api.chat`；新增 ChatApplication 与 MCP/HTTP 边界回归断言。
- 验证：服务端全量 1691 passed；`ruff check server/app server/tests` 通过；`git diff --check` 通过；脱敏扫描（Token/邮箱/IPv4/QQ 号）无命中。
- 提交：`20e0d16`；已推送 GitHub `origin/main`，本地 HEAD 与远程一致。
- 运行状态：systemd 服务、NapCat 与 searxng 未改变，未重启服务。
- 未完成：MCP 记忆/知识工具仍直接使用 legacy core/service，待下一阶段通过 application ports 收敛；QQ 入站方案、`group_directed` 与真实 @ 门禁归属仍待决定。

### 2026-09-16 — MCP 记忆/知识工具接入 application ports

- 代码/配置：新增 `app/application/ports.py` 组合根，HTTP ChatApplication 复用默认端口装配；MCP 记忆、知识、写入工具和 facts resource 改从 application ports 获取 Memory/Knowledge 实现；MCP 身份上下文改用 `app.identity`；新增 MCP 不得直接依赖 `app.core` 的架构断言；未改数据库 schema、配置和运行服务。
- 验证：MCP/架构定向测试 23 passed；服务端全量 1692 passed；`ruff check server/app server/tests` 通过；`git diff --check` 通过；脱敏扫描待提交前完成。
- 提交：本阶段代码与状态补记已提交并推送 GitHub（提交号见 Git 历史）。
- 运行状态：systemd 服务、NapCat 与 searxng 未改变，未重启服务。
- 未完成：fitness/novel/group application 门面迁移；QQ 入站方案、`group_directed` 与真实 @ 门禁归属仍待决定。

### 2026-09-16 — QQ 入站方案与真实 @ 门禁取证

- 代码/配置：未改服务端运行时代码或 NapCat 配置；核对现有 `/api/chat` 群作用域、QQ 身份 HMAC、`request_id` 幂等、`group_directed` 字段和 `qq_push` 出站契约，并回看已删除 QQ 插件的 fail-closed 真实 @ 判定与回复出口。
- 验证：确认当前没有 OneBot 入站消费者；历史判定仅接受前缀、框架唤醒标记、目标为本账号的 At、目标为本账号的 Reply，无法确认账号或引用目标时拒绝触发；现有群聊服务端已能按 `group_directed` 区分直达与非直达并在非直达默认关闭主动插话。
- 提交：本次取证记录随阶段提交。
- 运行状态：systemd 服务、NapCat 与 searxng 未改变，未重启服务。
- 未完成：需要选择自建轻量 OneBot 网关或第三方框架，并确定门禁在网关/服务端的唯一归属及 @ 后是否强制发送。

### 2026-09-16 — 自建 OneBot 薄网关实现

- 代码/配置：新增 `qq/onebot_gateway` 包，实现 OneBot v11 HTTP 事件解析、群白名单、真实 At/Reply/前缀 fail-closed 门禁、`group_directed` 映射、有限 follow-up、发送频率控制、QQ 身份 HMAC、`request_id` 幂等和 NapCat action 客户端；补充 CQ 码兼容、配置 self_id 缺失拒绝和有界状态存储；新增 13 项契约测试、systemd 模板、`.env.example` 网关配置说明，并同步更新 README、`docs/QQ_OPS.md` 与 `docs/OPS.md`。
- 验证：网关定向测试 13 passed；服务端全量 1705 passed；`ruff check app tests`、`git diff --cached --check` 已通过；最终 staged 脱敏扫描和提交前验证待完成。
- 提交：未提交。
- 运行状态：未修改 NapCat 运行配置，未安装/启用网关 systemd，未重启任何现网服务。
- 未完成：完成全量回归和脱敏检查后提交并推送；真实 QQ 消息验收需后续在部署机应用配置后进行。
