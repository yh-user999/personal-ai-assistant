# AGENTS.md

本文件是本项目给新接入的模型 Agent 的快速上下文入口。开始任何代码、配置、部署或文档工作前先读本文件；完成阶段性工作后更新“当前状态”和“变更记录”。

## 1. 项目一句话

这是一个“服务器大脑 + 可选 Windows 客户端 + QQ 入口 + 小说工作台”的个人 AI 助手项目。服务器负责聊天、记忆、知识库、图片、提醒、任务和小说工作台；Windows 负责可选的本地采集、桌面端和远程执行。QQ 侧保留 NapCat 登录与 OneBot 能力，并由仓库内独立的 `qq/onebot_gateway` 薄网关承接入站；MaiBot 与 AstrBot 两套第三方机器人链路已于 2026-09-15 彻底删除。

## 2. 代码地图

- `server/`：FastAPI 服务端、SQLite、聊天流水线、LLM/Embedding、记忆、知识库、提醒、小说工作台。
- `server/app/ai_news/`：每日 AI 资讯搜索、来源约束、结构化摘要、SQLite 归档和 `/api/ai-news` 路由。
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

网关已在仓库实现并应用到部署机与 NapCat 运行配置：

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

## 3. 已核对运行事实（2026-09-18）

### 服务器

- GitHub 仓库：`yh-user999/personal-ai-assistant`。
- 本次核对的实际部署仓库：`/opt/personal-ai-assistant`。
- FastAPI 实际运行目录：`/opt/personal-ai-assistant/server`，端口 8000；当前由 systemd 单元 `personal-assistant.service` 管理（`User=paa`、`EnvironmentFile` 提供 `PORT=8000`、enabled 开机自启），手工 `setsid nohup` 方式已停用。
- 服务日志：`journalctl -u personal-assistant`（旧 `/tmp/assistant.log` 已停止写入）；检查优先使用 `/api/health` 和 `/api/ready`。
- 部署仓库同步：`paa` 直接对 `/opt/personal-ai-assistant` 执行 `git pull --ff-only`（SSH 凭据在该仓库 `.ssh/`，权限 600、直连 GitHub、不进入 Git）；命令见第 6 节。
- 最近已部署业务功能提交：`96c8d05`（修复 QQ OneBot action 专用路径与真实发送回执校验）；部署仓库已同步。权威状态始终以 `git rev-parse HEAD` 和 `git ls-remote origin refs/heads/main` 为准，不要只相信本段文字。

### QQ / NapCat（与服务器同机，2026-09-15 核对）

- NapCat 容器：`napcat`，`network=host`，`restart=always`；持久化目录 `/opt/napcat/{qq_config,cache,config,qq-login}`。
- QQ 登录态保留在 `/opt/napcat/qq_config`，清理过程中未重新扫码。
- NapCat OneBot 配置中当前只保留 `httpServers[xy-push]`；原 `websocketServers[maibot]` 与 `wsReverse/websocketClients[astrbot]` 条目已删除。`httpClients[xy-gateway]` 指向本机 `3101/onebot`；NapCat 4.18.19 实测不发送其 client token，因此网关保持仅回环监听，部署 `.env` 的入站 token 留空。
- 网关 systemd 单元 `personal-qq-gateway.service` 已于 2026-09-16 安装并启用（`User=paa`，仅监听 `127.0.0.1:3101`，入口 `python -m qq.onebot_gateway`）。
- 本机端口现状：`6099`（NapCat WebUI）开放；`3001`（原 MaiBot 正向 WebSocket）已随配置删除而关闭；`3101` 由 `personal-qq-gateway` 监听（仅回环）。
- QQ 登录态已通过 NapCat WebUI 重新扫码恢复；真实私聊、群 At、引用 Reply、非直达静默、一次免 At 续话、重复事件幂等和 NapCat 发送均已验收。群文字前缀按用户选择保持关闭。
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
- QQ 薄网关已在部署机安装并启用：`personal-qq-gateway.service` active（127.0.0.1:3101），NapCat `httpClients[xy-gateway]` 已配置生效；OneBot action 使用 `/{action}` 专用路径并要求发送返回有效 `message_id`，`.env` 密钥与配置仅存本机。
- 每日 AI 资讯日报已部署：每天 08:00（Asia/Shanghai）复用现有 SearXNG 与聊天 LLM 搜索并生成来源约束摘要，独立表按主体/日期幂等归档；`/api/ai-news` 仅 `owner`/`internal`，聊天快捷读取仅主人私聊，QQ 推送未配置时非阻塞跳过。
- 受控私聊/群聊联网检索已实现并部署：书名/作品查询规则、HTTP(S) 来源硬门槛、群真实直达门禁、按群冷却/小时限额/单次预算、失败释放预留、作品资料来源过滤与 LLM 故障来源兜底均已纳入；生产 `GROUP_WEB_SEARCH_ENABLED` 已按用户确认开启，当前真机验收受上游生成接口 503 阻塞。
- 小说研究来源质量门禁已完成：起点等一手作品页优先，百科/结构化资料补充，章节聚合/转载站不进入事实资料块；回答只能据本轮摘录，来源正文不足或仅有低质来源时固定降级。
- 语义 planner 主导联网路由改造已完成：结构化计划携带 route/research_kind/subject/question/queries/source_preference，研究执行消费语义查询，规则仅作安全合并与失败 fallback；来源门禁、群作用域、社交动作和有限续话均消费结构化计划。

### 未完成/明确限制

- QQ 文本真机链路已验收；当前群聊仍只允许真实 At、引用 Reply 和服务端明确开启的 90 秒/1 条续话，文字前缀按用户选择保持关闭。
- `QQ_PUSH_URL`/`QQ_PUSH_TOKEN`/`QQ_ADMIN_ID` 在部署 `.env` 长期为空（提醒与 AI 日报 QQ 推送未启用、服务端主人身份为 `owner` 哨兵）；网关已改用 `QQ_GATEWAY_ONEBOT_URL/TOKEN`、`QQ_GATEWAY_OWNER_ID` 接线。若要恢复推送并把主人身份切到数字 QQ，需要迁移 owner 历史数据并重启服务，属待决事项。
- 网关当前只处理文本事件；图片、语音、视频和文件安全忽略，媒体入口另行设计。
- 服务端已由 systemd 管理（`EnvironmentFile` 固定 `PORT=8000`），不再受宿主 `PORT` 继承问题影响；仅手工调试时才需要 `env -u PORT PORT=8000 .venv/bin/python run.py`。
- 群聊实时公网搜索的生产开关已按用户确认开启：公共 HTTP(S) 来源、来源审校、按群限额、12 秒默认单次预算、失败释放和作品资料相关性过滤均已部署；生产 helper 已命中 5 个相关来源，但《没钱修什么仙》真机完整回答仍待上游生成接口恢复。
- 生产小说只读验收暂受搜索上游阻塞：SearXNG 首页可达，但小说 JSON 搜索请求超时，起点作品页当前也未取得正文；服务按无来源安全降级，待搜索后端恢复后重跑验收。
- QQ 网关入站/出站与服务端链路稳定；2026-09-19 最近真机请求的上游 `chat/completions` 连续返回 503，已增加“已有来源时直接返回来源摘要”的 LLM 故障兜底；目标模型最小真实探针仍返回 503。
- 原 opencode 聊天通道于 2026-09-17 达到月度额度上限；当前聊天已切换至受信任 HTTPS New API 的 `gemini-3.8-flash-high`，如需切回需使用仓外旧配置备份。
- `/api/ready` 曾因服务进程内单个长连接出现 FTS5 视图异常而 503；本次再次出现 `memories_fts` malformed inverted index 后，已在仓外在线备份副本验证并重建生产 `memories_fts`/`knowledge_fts`，完整性检查恢复正常。
- `qq/`、`deploy/maibot/` 与根 `data/` 已于 2026-09-15 从仓库删除；@ 判定语义见 `docs/QQ_MENTION_REFERENCE.md`，原实现可在 Git 历史中查阅。
- 模块化单体尚未完成 fitness/novel/group application 门面迁移。
- 2026-09-16 架构审计待办（按优先级）：① 数据访问未收口——66 个文件直接写 SQL、63 个文件各自 `connect()`，建议与 fitness/novel/group 门面迁移合并，按包建 repository；② `models/database.py`（1631 行）混了 schema/迁移/连接/完整性检查，建议拆分。包级循环棘轮基线与 `/api/ready` 完整性缓存已完成（见 2026-09-16 变更记录）。

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
- 验证：网关定向测试 13 passed；服务端全量 1705 passed；`ruff check app tests`、`git diff --cached --check` 和 staged 脱敏扫描均通过。
- 提交：实现 `b986d68`、状态记录 `b53e3d5`，均已推送 GitHub `origin/main`。
- 运行状态：未修改 NapCat 运行配置，未安装/启用网关 systemd，未重启任何现网服务。
- 未完成：真实 QQ 消息验收需后续在部署机应用配置后进行。

### 2026-09-16 — QQ 薄网关部署接线（部署机）

- 代码/配置：部署仓库 `/opt/personal-ai-assistant` fast-forward 至 `e954cf4`；`.env` 追加 `QQ_GATEWAY_*`（随机入站 token、群白名单 `*`、`QQ_GATEWAY_ONEBOT_URL/TOKEN` 指向本机 NapCat 3100、`QQ_GATEWAY_OWNER_ID` 取自本机旧插件配置备份）；NapCat `onebot11` 配置新增 `httpClients[xy-gateway]` → `127.0.0.1:3101/onebot`；安装并启用 `personal-qq-gateway.service`。网关首次启动因部署 `.env` 的 `QQ_PUSH_URL` 为空而失败，因此网关改为优先读专用变量（提交 `e954cf4`），避免牵动服务端提醒推送与主人身份语义；NapCat 配置与 `.env` 改动前已备份到 `/opt/cleanup-backup-20260916T064709Z/`（0700）。
- 验证：网关 `/health` 200；入站 token 401/200、媒体与超长安全忽略冒烟通过；NapCat 容器重启后 `httpClients` 配置保留；`/api/ready` 曾因服务进程内单个长连接的 FTS5 视图异常返回 503（同一库的新连接与备份副本均为 `ok`，写锁无滞留），重启 `personal-assistant` 后并发 8/8 `ready`；日志确认 09-16 00:04 仍在接收群消息。
- 提交：`e954cf4`（变量调整）与本次状态记录提交；均已推送 GitHub `origin/main`。
- 运行状态：`personal-assistant` 与 `personal-qq-gateway` 均 active；NapCat 容器运行中，但 QQ 会话于 09-16 09:57 被踢下线（`KickedOffLine`），等待手机扫码重新登录；登录前网关收不到事件。
- 未完成：扫码后做真实消息验收（真实 At/Reply/前缀、非直达静默、续话、幂等与 NapCat 发送）；恢复提醒推送与主人数字身份（`QQ_PUSH_*`/`QQ_ADMIN_ID`）属待决事项；上游 LLM 429 月度额度约 2 天后重置，期间回复可能失败。

### 2026-09-16 — 架构审计与群/私聊话题补偿隔离修复

- 代码/配置：只读审计服务端架构（依赖矩阵、SQL 分布、异常处理、模块级状态、测试覆盖），结论记入第 4 节待办；修复审计发现的隔离缺口——`core/memory.py` 的 `_topic_boost_map`/`_topic_boost_for` 补群作用域并由 `search()` 透传当前 `group_id`，使话题活跃度补偿不再跨群/私聊统计。
- 验证：新增 `test_topic_boost_is_scoped_to_current_conversation`；在临时库上以等效 SQL 对比证明修复前三个作用域返回相同话题集（互相污染）、修复后各自隔离；服务端全量 1707 passed、`ruff check app tests` 通过、`git diff --check` 通过、staged 脱敏扫描 0 命中。
- 提交：见本次提交号（下方报告）。
- 运行状态：未重启服务；本次改动只影响检索排序权重，不改 schema 与 HTTP 契约。
- 未完成：审计列出的数据访问收口、包级循环纳入基线、`database.py` 拆分、`/api/ready` 完整性检查降频均未实施；QQ 登录与链路验收按用户要求暂缓。

### 2026-09-16 — QQ 网关端到端回归与上游失败分类修复

- 代码/配置：新增 `server/tests/test_qq_gateway_end_to_end.py`（9 项），网关 `ChatClient` 经 `httpx.ASGITransport` 直连真实 FastAPI，只桩 `llm.chat` 与 embedding，覆盖真实 At、Reply 目标判定、前缀、纯名字静默、非白名单群静默、重复事件不二次发送、每群小时上限、群作用域落库隔离、配置 self_id fail-closed；据此把待人工验收面压缩到「NapCat 真机送达」。修复端到端暴露的缺陷：`qq/onebot_gateway/chat.py` 新增 `ChatConfigError`（401/403 与缺 token），`app.py` 对该类失败静默并记 error 日志，仅临时故障才回「服务暂时不可达」，避免把服务器鉴权配置故障广播给群成员；`docs/QQ_OPS.md` 补充两类失败的排障口径。
- 验证：端到端 9 passed、网关契约 14 passed；服务端全量 1716 passed；`ruff check app tests` 与 `ruff check qq` 均通过；反向验证确认错误 HMAC 会被真实服务端 401 拒绝（证明鉴权确实在链路内生效）。
- 提交：见本次提交号（下方报告）。
- 运行状态：部署仓库已 fast-forward 至 `e1dfe00`，`personal-qq-gateway` 与 `personal-assistant` 均已重启并加载新代码（网关 `ChatConfigError` 就位、`_topic_boost_map/_topic_boost_for` 已带群作用域并由 `search()` 透传）；`/api/health` 200、`/api/ready` 并发 8/8 ready、网关 `/health` 200，日志无异常。未改 HTTP 契约与数据库 schema。
- 未完成：真机送达仍需用户扫码后确认（用户已暂缓登录）；架构审计的数据访问收口、包级循环基线、`database.py` 拆分、`/api/ready` 降频未实施。

### 2026-09-16 — 架构债棘轮基线与 /api/ready 完整性缓存

- 代码/配置：`tests/test_architecture_boundaries.py` 新增两项棘轮断言——6 组包级双向耦合与 229 处函数体内延迟导入记为基线，只禁止新增、消除后须收紧清单（含收紧提示）；`app/main.py` 给 `/api/ready` 的全库 `PRAGMA integrity_check` 加 300 秒 TTL 缓存（成功才缓存、失败不缓存、提供手动失效入口），轻量检查仍每次实时执行；`tests/test_smoke.py` 补缓存命中与失败不缓存两个行为用例；`docs/OPS.md` 补探活缓存说明。
- 验证：棘轮双向有效性经等效验证（注入新增循环会失败、消除会提示收紧）；smoke 7 passed；服务端全量 1719 passed；`ruff check app tests` 通过；staged 脱敏扫描 0 命中。
- 提交：`a900227`；已推送 GitHub `origin/main`。
- 运行状态：未同步部署机、未重启服务；改动为测试基线与探活缓存，不改 HTTP 契约与数据库 schema。
- 未完成：sync 部署机以启用 ready 缓存；审计的 repository 收口与 `database.py` 拆分未实施；QQ 登录与链路验收按用户要求暂缓。

### 2026-09-16 — /api/ready 缓存部署验收

- 代码/配置：部署仓库 `/opt/personal-ai-assistant` 由 `e1dfe00` fast-forward 至 `ec7658f`（包含 `a900227` 的架构基线与探活缓存代码）；仅重启 `personal-assistant`，未改 QQ 网关配置。
- 验证：服务启动完成后 `personal-assistant` active；`/api/health` 返回 ok；`/api/ready` 返回 ready，database schema 17、完整性与外键检查正常，failures/degraded 为空；`personal-qq-gateway` active 且 `/health` ok。
- 提交：`ec7658f`；部署仓库与 GitHub `origin/main` 一致。
- 运行状态：服务已加载 300 秒 TTL 的 `/api/ready` 完整性缓存；QQ 登录与网关真机验收状态不变。
- 未完成：按包建 repository 的迁移方案待确认；`database.py` 拆分未实施；QQ 登录与链路验收按用户要求暂缓。

### 2026-09-16 — 首批健身结构化数据 repository 迁移

- 代码/配置：新增 `server/app/fitness/repository.py`，收口健身动作目录、食品营养、饮食记录、训练计划/会话/训练组/身体指标及统计查询；`catalog.py`、`nutrition.py`、`training.py` 不再直接导入数据库模块；新增仓储契约测试与结构化健身边界棘轮断言；更新 `docs/模块开发指南.md`。
- 验证：健身全域（含 API、MCP）68 passed；仓储/架构边界定向 31 passed；服务端全量 1722 passed；`ruff check app tests`、`git diff --check` 与 staged 脱敏扫描通过；未改数据库 schema、HTTP/MCP 契约、QQ 配置。
- 提交：`e08c75a`；已推送 GitHub `origin/main`，部署仓已 fast-forward 同步。
- 运行状态：部署仓健身目录属主已修正；`personal-assistant` 重启后 active，`/api/health`、`/api/ready` 均正常；`personal-qq-gateway` 未重启但 active，网关 `/health` 正常；旧 `fitness/service.py` 自由文本台账/知识卡保留兼容路径。
- 未完成：Novel/Group repository 迁移及 `database.py` 拆分未实施；部署仓保留一个同步前的已核验 stash；QQ 登录与链路验收按用户要求暂缓。

### 2026-09-17 — 切换聊天 LLM 到 HTTPS New API

- 代码/配置：部署机 `.env`（不入 Git）切换聊天基地址至受信任 HTTPS OpenAI 兼容网关、模型 `gemini-3.8-flash-high`，令牌来自本机 `NEW_API_TOKEN`；旧 `.env` 以 600 权限备份在仓外。
- 验证：模型列表 HTTPS 严格证书校验 HTTP 200，目标模型可用；`personal-assistant` 重启后 active；`/api/ready` ready，database/scheduler/llm/vector 全 ok；真实最小聊天调用返回预期文本；QQ 网关未改动。
- 提交：见本次提交号（下方报告）；已推送 GitHub。
- 运行状态：聊天已恢复；QQ 登录与真实链路验收仍暂缓。
- 未完成：QQ 扫码验收、Novel/Group repository 迁移和 `database.py` 拆分未实施；原 opencode 配置保留在仓外备份。

### 2026-09-18 — 每日 AI 资讯日报部署验收

- 代码/配置：新增 `server/app/ai_news/` 领域包、schema 18 的 `ai_news_digests` 表、每天 08:00（Asia/Shanghai）调度、`/api/ai-news` 查询/生成接口和主人私聊快捷读取；复用现有 SearXNG 与主 LLM，不新增外部 API Key；QQ 配置可用时私聊推送，未配置时非阻塞跳过。
- 验证：服务端全量 1737 passed，`ruff check app tests`、架构棘轮、迁移/权限/幂等/失败回归与 staged 脱敏扫描通过；部署后真实生成 2026-09-18 日报（8 条、32 个来源），重复生成幂等跳过，latest/history API 与聊天快捷读取均通过。
- 提交：`83d740a`；已推送 GitHub `origin/main`，部署仓已 fast-forward 同步。
- 运行状态：仅重启 `personal-assistant`；`/api/health`、`/api/ready` 与 QQ 网关 `/health` 正常，数据库 schema 18，下一次自动任务为 2026-09-19 08:00；QQ 网关未重启、QQ 配置未修改。
- 未完成：AI 日报 QQ 推送随既有 `QQ_PUSH_*`/`QQ_ADMIN_ID` 待决事项保持未启用；QQ 扫码与真机链路验收继续按用户要求暂缓。

### 2026-09-18 — QQ 登录恢复、OneBot 发送修复与真机验收

- 代码/配置：恢复 NapCat QQ 扫码登录；现场抓包确认 NapCat 4.18.19 reverse HTTP 客户端不发送配置的 client token，网关仅监听回环且部署入站 token 留空；修复 OneBot 客户端错误使用根路径封装动作的问题，统一调用 `/{action}` 并要求发送返回有效 `message_id`；文字触发前缀按用户选择保持关闭。
- 验证：QQ 定向回归 34 passed、服务端全量 1738 passed、Ruff 与 staged 脱敏扫描通过；真机验证私聊闭环、真实 At、引用 Reply、非直达静默、12 秒内一次免 At 续话、重复事件只发送一次。修复后网关统计 148 次上报均为 200、无 401/鉴权/上游/发送错误，服务端 8 次聊天请求均为 200。
- 代码提交：`96c8d05`；本次状态记录随补记提交推送 GitHub，并同步部署仓。
- 运行状态：仅重启 NapCat（恢复登录）与 `personal-qq-gateway`（加载配置和发送修复）；`personal-assistant` 未重启且持续 ready，网关 `/health` 正常。
- 未完成：`QQ_PUSH_*`/`QQ_ADMIN_ID` 与主人历史数据迁移仍待用户决定；群聊公网检索仍按安全策略关闭。近期 LLM 偶有 30–56 秒延迟，但网关未见故障。

### 2026-09-18 — 生产 FTS 索引完整性修复

- 代码/配置：未改仓库业务代码；为生产 SQLite 创建仓外在线备份，在维护窗口重建 `memories_fts` 与 `knowledge_fts` 索引，修复 `/api/ready` 报告的 FTS5 inverted index 异常。
- 验证：重建后 `PRAGMA integrity_check` 返回 `ok`、`foreign_key_check` 为 0；记忆 4107 条与 FTS 行数一致、知识块 272 条与 FTS 行数一致；`/api/health`、`/api/ready`、QQ 网关 `/health` 均正常。
- 提交：本次状态记录待提交；无业务代码提交。
- 运行状态：短暂停止并恢复 `personal-assistant` 与 `personal-qq-gateway`；两项服务 active，QQ 事件恢复 200。
- 未完成：无新增；外部配置与 QQ 推送/主人数字身份待决事项不变。

### 2026-09-18 — 受控私聊/群聊联网检索实现

- 代码/配置：新增默认关闭的群联网配置与进程内按群限额；补充书名/作品确定性检索规则、精确书名→有限扩展查询、HTTP(S) 来源过滤与链接/时间注入；群路径仅在真实直达消息下联网，不进入主人知识库、画像或调查链路。
- 验证：定向回归 132 passed；服务端全量 1752 passed；服务端与 QQ 网关 Ruff、compileall、git diff --check 通过；staged 高置信脱敏扫描 0 命中；部署后 `/api/health`、`/api/ready` 和 QQ 网关服务状态正常。
- 提交：`5dab307`；已推送 GitHub，部署仓已 fast-forward 同步。
- 运行状态：仅重启 `personal-assistant`；未重启 QQ 网关，生产 `GROUP_WEB_SEARCH_ENABLED` 保持关闭。
- 未完成：用户确认后开启群联网并真机验收《没钱修什么仙》来源、无来源降级与限额行为；QQ 推送/主人数字身份仍按既有决策项阻塞。

### 2026-09-19 — 作品检索命中优化与上游生成故障兜底

- 代码/配置：作品查询改走通用网页类别，保留标题问号，首轮使用“作品简介/剧情/设定”词并过滤无关结果；群默认预算调为 12 秒；LLM 生成失败但已有可靠来源时直接返回来源摘要与链接，不凭印象补写。
- 验证：服务端全量 1756 passed；定向联网/流水线 150 passed；Ruff、compileall、git diff --check 和 staged 高置信脱敏扫描通过；生产 helper 返回 5 个相关 HTTPS 来源，`/api/ready` 200。
- 提交：`33ab7a4`、`4e87d26`、`74e646c`、`fda5c4c`、`5f8cf0c`、`7274275`、`28b9e8d`、`35c6e50`、`08db4cb` 均已推送 GitHub，部署仓已同步至 `08db4cb`。
- 运行状态：仅重启 `personal-assistant`；`personal-qq-gateway` 未重启且 active；生产 `GROUP_WEB_SEARCH_ENABLED=true`，未改 `QQ_PUSH_*`/QQ 身份配置。
- 未完成：真实 QQ 查询已确认网关与服务端 200，但上游目标模型最小 `chat/completions` 探针仍返回 503；等待上游恢复后再做最终带来源回复验收。QQ 推送/主人数字身份继续 blocked。

### 2026-09-19 — 通用 Web Research 与 GitHub 项目 Provider

- 代码/配置：复用现有 SearXNG、SSRF 安全抓页、正文清洗和 Evidence 链路，新增通用研究分类/有限研究编排与 GitHub 公开仓库、README、Release、Issue/PR Provider；明确小说、知识、文档、项目、URL 查询不能被语义 planner 改成闲聊；新增无密钥研究/GitHub 配置模板，不默认写入长期记忆。
- 验证：服务端全量 1769 passed；通用研究/GitHub/聊天联网定向回归、Ruff、compileall 通过；GitHub Provider 公开 API 只读冒烟返回 3 个仓库；工作区 SearXNG 未配置时小说研究按 `no_sources` 安全降级。
- 提交：待 staged 脱敏扫描后提交。
- 运行状态：尚未部署或重启生产服务；QQ 配置、群联网开关和主人数字身份未改。
- 未完成：生产部署与健康检查待本阶段提交；复杂 JavaScript 页面暂不引入浏览器抓取；QQ 推送/主人数字身份继续 blocked。

### 2026-09-19 — GitHub 项目自然语言查询修正

- 代码/配置：生产只读验收发现中文项目查询原样发送给 GitHub API 会返回空结果；新增项目名/技术词重写，明确 Release/版本请求时读取匹配仓库的 README 与 Release 来源。
- 验证：服务端全量 1770 passed；项目查询与 GitHub Provider 定向回归 10 passed；生产直接查询 `Crawl4AI` 返回 3 个公开仓库，中文自然语言回归通过。
- 提交：`6ae27e8`；已推送 GitHub。
- 运行状态：部署仓已 fast-forward 至 `6ae27e8`，随后随下一阶段更新至 `088fced`；`personal-assistant` ready，QQ 网关 active；QQ 配置、群联网开关和主人数字身份未改。
- 未完成：后续快速首轮优化已在下一条记录完成；复杂 JavaScript 页面暂不引入浏览器抓取；QQ 推送/主人数字身份继续 blocked。

### 2026-09-19 — 作品研究快速首轮优化

- 代码/配置：通用小说研究首轮改为“纯书名+问号”，命中后再扩展作品简介/剧情/设定，避免 SearXNG 首轮附加词偶发超时；保留旧群聊查询链路不变。
- 验证：生产只读研究返回 6 个来源，首轮查询为“没钱修什么仙？”，耗时约 10.6 秒；3 个正文页面失败但来源摘要保留；服务端全量 1770 passed。
- 提交：`088fced`；已推送 GitHub，部署仓已 fast-forward 同步。
- 运行状态：仅重启 `personal-assistant`；`/api/ready` ready，`personal-qq-gateway` active；QQ 配置、群联网开关和主人数字身份未改。
- 未完成：复杂 JavaScript 页面正文提取备用方案进入后续 todo；QQ 推送/主人数字身份继续 blocked。

### 2026-09-19 — 语义 planner 主路由改造
- 代码/配置：响应计划新增 route/research_kind/subject/research_question/research_queries/source_preference 与有限 followup 字段；私聊、群聊研究、社交动作、语气和续话消费结构化计划；规则层仅保留安全合并与 planner 失败 fallback；来源无结果固定降级；新增语义查询、群隔离、访客权限和副作用边界回归。
- 验证：服务端全量 1776 passed；定向语义/研究/群聊回归通过；Ruff、compileall、git diff --check 和 staged 高置信脱敏扫描通过；部署后 `/api/health`、`/api/ready`、QQ 网关 `/health` 均正常，数据库 schema 18。
- 提交：`9e707e7`；已推送 GitHub，部署仓已 fast-forward 同步。
- 运行状态：`personal-assistant` 已重启并 active；QQ 网关未重启且保持 active；QQ 配置、群联网生产开关和推送配置未改。
- 未完成：复杂 JavaScript 页面正文提取与 QQ 推送/主人数字身份继续 blocked；无本阶段新增阻塞。

### 2026-09-19 — 小说检索来源质量与证据边界
- 代码/配置：新增小说来源后台分层与排序，优先一手作品页和结构化资料，过滤章节聚合/转载站；私聊、群聊和旧兼容检索路径统一只注入可靠来源；要求来源正文/摘要存在，缺可靠来源时固定降级；小说提示词明确只能依据本轮摘录，内部等级不展示；补充起点优先、百度百科补充、转载过滤、正文回归、低质来源降级、提示词和群限额测试。
- 验证：定向回归 186 passed；服务端全量回归 1782 passed；Ruff、compileall、git diff --check 通过；暂存区脱敏扫描通过（11 个文件，新增行高置信规则 0 命中）。生产部署后 `/api/health` 与 `/api/ready` 均正常。
- 提交：代码 `e6a9940` 已推送 GitHub；本次状态补记随当前文档提交。
- 运行状态：部署仓已同步 `e6a9940`，`personal-assistant` 已重启并 active；搜索后端首页可达，但小说 JSON 搜索请求超时，起点作品页未取得正文，服务按无来源安全降级。
- 未完成：生产小说只读验收被搜索上游阻塞，恢复后需重跑；复杂 JavaScript 页面提取和 QQ 推送/主人数字身份继续暂缓。
