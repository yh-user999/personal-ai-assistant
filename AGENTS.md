# AGENTS.md

本文件是本项目给新接入的模型 Agent 的快速上下文入口。开始任何代码、配置、部署或文档工作前先读本文件；完成阶段性工作后更新“当前状态”和“变更记录”。

## 1. 项目一句话

这是一个“服务器大脑 + 可选 Windows 客户端 + QQ 入口 + 小说工作台”的个人 AI 助手项目。服务器负责聊天、记忆、知识库、图片、提醒、任务和小说工作台；Windows 负责可选的本地采集、桌面端和远程执行。QQ 侧当前只保留 NapCat 登录与 OneBot 能力，MaiBot 与 AstrBot 两套第三方机器人链路已于 2026-09-15 彻底删除，新的 QQ 接入方案尚未确定。

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
- `docs/QQ_MENTION_REFERENCE.md`：历史 @ 判定语义摘录（fail-closed 判据、组件字段兼容坑、限流取舍），QQ 接入重构时的参考。
- `docs/QQ_OPS.md`：QQ/NapCat 运维与安全边界；其中 AstrBot 相关章节已失效，需以本文件的“已核对运行事实”为准。
- `docs/OPS.md`：服务端、Windows 客户端和组件排障；其中部分旧路径需以本文件的“已核对运行事实”为准。
- `docs/模块开发指南.md`：目录归属、四件套契约（service/api/建表迁移/聊天触发词/测试）与新增模块注册流程；新增领域模块前先读。
- `spec://tasks.json`：当前任务队列；修改前必须先读，再就地编辑。

### 2.1 当前请求链路

```text
QQ/NapCat 登录（保留）
  → OneBot 事件（当前无下游消费者）
```

服务端聊天流水线本身完好，但 QQ 入口已断开：

- `server/app/chat/pipeline.py` 是聊天编排骨架；群聊行为编排拆在 `server/app/group/turn.py`，把心流、主动插话、检索、生成和审校串起来。
- `server/app/group/interjection.py` 负责非直达消息的确定性兴趣评分、冷却、小时上限和 reservation。
- `server/app/group/heartflow.py` 负责群级活跃度、能量和连续回复限制；`relationship.py` 负责抽象互动熟悉度；`context.py` 负责短期话题上下文。
- `server/app/chat/followup.py` 生成有限的 `interaction` 元数据，原先由 QQ 插件的 `FollowupStore` 消费；当前没有消费者。
- `server/app/chat/review.py` 是候选回复的最终审校出口；没有可靠来源的群外部事实必须降级。

### 2.2 新架构待决问题（2026-09-15）

清理后需要重新决定的点，尚未有结论：

1. QQ 接入由自建轻量 OneBot 网关承担，还是重新引入第三方框架。
2. 群聊“是否发言”的决策放在网关还是服务端，避免再次出现两层重复判定。
3. “仅真实 @ 回复”作为确定性门禁的落点，以及 @ 后是否强制发送。
4. 服务端已有的 QQ 接入契约（`group_directed`、QQ 身份 HMAC 头、`request_id` 幂等、`qq_push` 发送出口）在新方案里如何复用。

## 3. 已核对运行事实（2026-09-13）

### 服务器

- GitHub 仓库：`yh-user999/personal-ai-assistant`。
- 本次核对的实际部署仓库：`/opt/personal-ai-assistant`。
- FastAPI 实际运行目录：`/opt/personal-ai-assistant/server`，端口 8000；当前采用手工启动的 `run.py`，启动时显式固定端口 8000，避免继承宿主的其他端口环境。
- 服务日志：`/tmp/assistant.log`；检查优先使用 `/api/health` 和 `/api/ready`。
- 最近已部署功能提交：`7c934b1`（群聊通用上下文续话）。权威状态始终以 `git rev-parse HEAD` 和 `git ls-remote origin refs/heads/main` 为准，不要只相信本段文字。

### QQ / NapCat（与服务器同机，2026-09-15 核对）

- NapCat 容器：`napcat`，`network=host`，`restart=always`；持久化目录 `/opt/napcat/{qq_config,cache,config,qq-login}`。
- QQ 登录态保留在 `/opt/napcat/qq_config`，清理过程中未重新扫码。
- NapCat OneBot 配置中当前只保留 `httpServers[xy-push]`；原 `websocketServers[maibot]` 与 `wsReverse/websocketClients[astrbot]` 条目已删除。
- 本机端口现状：`6099`（NapCat WebUI）开放；`3001`（原 MaiBot 正向 WebSocket）已随配置删除而关闭。
- MaiBot 与 AstrBot 已彻底删除：容器 `maim-bot-core`、镜像 `sengokucola/maibot:latest`、目录 `/opt/maibot`、`astrbot.service` 单元与 `/opt/astrbot` 均不存在。
- 清理前的配置类文件已转存到服务器本地 `0700` 备份目录 `/opt/cleanup-backup-<UTC 时间戳>/`，仅保留在服务器，不进入 Git。
- 当前运行中的容器只有 `napcat` 与 `searxng`。

## 4. 当前功能边界

### 已完成

- 身份问题走确定性口径：群聊问“你是谁/你叫什么”等应直接回答“我是小月”。
- 群聊短时上下文续话：同一群、同一用户在小月成功直达回复后的短窗口内，可免 @ 续接一次；服务端显式澄清提示支持书名、链接、简介等补充请求。
- 群作用域不自动注入私聊画像；群级表达学习不应写入个人画像。
- 服务端与历史 QQ 插件源码均有回归测试、lint 和脱敏检查。
- MaiBot 与 AstrBot 两套第三方机器人链路已彻底清理，QQ 登录态保留在 NapCat。
- 代码结构重组完成：健身/群聊/小说各自成领域包（`app/fitness`、`app/group`、`app/novel`），`app/chat/pipeline.py` 只留编排骨架，群聊轮次编排在 `app/group/turn.py`；新增模块流程见 `docs/模块开发指南.md`。

### 未完成/明确限制

- QQ 入口当前断开：NapCat 仍在线，但没有任何下游消费 OneBot 事件，群聊不会有任何回复。
- 新 QQ 接入架构尚未确定；“仅真实 @ 回复”的确定性门禁、以及决策放在网关还是服务端，都还没有结论。
- 启动 FastAPI 必须显式固定端口：`run.py` 读取环境变量 `PORT`，会继承宿主 shell 里已有的 `PORT`（实测被继承为 7778 并因端口占用启动失败）。用 `env -u PORT PORT=8000 .venv/bin/python run.py` 启动。
- 群聊实时公网搜索当前被策略禁用：搜索后端本身可配置，但群检索路径不联网，只允许作用域内历史/记忆；要开放群联网，必须新增“公共来源、来源审校、群/用户限额、Token 熔断”的受控策略，不能只改一个开关。
- 此前上游 LLM 曾出现 HTTP 429（月度额度耗尽）；新链路接入后仍需重新确认可用额度。
- 群聊真实消息回复、身份回答与通用续话验收全部未通过，且在新 QQ 接入方案落地前没有运行环境可验收。
- `qq/`、`deploy/maibot/` 与根 `data/` 已于 2026-09-15 从仓库删除；@ 判定语义见 `docs/QQ_MENTION_REFERENCE.md`，原实现可在 Git 历史中查阅。

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

启动服务端（必须显式固定端口，避免继承宿主 `PORT`）：

```bash
cd /opt/personal-ai-assistant/server
env -u PORT PORT=8000 setsid nohup .venv/bin/python run.py </dev/null >/tmp/assistant.log 2>&1 &
```

检查 QQ 侧运行状态（当前只剩 NapCat）：

```bash
docker ps -a --format '{{.Names}}|{{.Status}}'
docker inspect napcat --format 'status={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}'
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
