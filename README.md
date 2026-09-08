# 个人智能助手

一个面向个人使用的 AI 助手项目：把聊天、长期记忆、知识库、图片识别、提醒、桌面机器人、QQ 接入和小说写作工作台组合在一起。

它采用“服务器 + 可选 Windows 客户端”的结构：

- 服务器负责保存数据、调用模型、处理聊天、运行定时任务和小说生成任务。
- Windows 采集器负责可选的本地行为采集、心跳和远程执行器。
- Windows 桌面端提供悬浮机器人、聊天面板、托盘和本地快捷操作。
- QQ/AstrBot 插件提供手机端私聊入口。
- 浏览器可以直接打开聊天页和小说工作台。

> 这不是一个注册账号即可使用的 SaaS，也不是开箱即用的聊天机器人。你需要准备自己的服务器、模型服务、密钥和网络访问方式。

## 先看结论

如果你是第一次接触这个项目，可以按下面的顺序理解：

1. 只想在浏览器聊天：只部署 `server/`。
2. 想在 Windows 桌面上使用悬浮机器人：再部署 `desktop/`。
3. 想让助手了解 Windows 上的窗口、浏览器或 Git 活动：再启用 `collector/`；这些采集开关默认关闭。
4. 想用手机 QQ：再配置 NapCat、AstrBot 和 `qq/astrbot_plugin_xy/` 插件。
5. 想写小说：打开服务器提供的 `/novel/` 小说工作台，不需要 Windows 电脑一直开着。服务器在线时，项目、章节、生成任务和进度都保存在服务器上。

电脑关机时，服务器上的聊天、知识库、提醒、小说数据和已提交的小说生成任务仍然可以运行；依赖 Windows 的本地文件操作、桌面快捷启动和行为采集会暂时不可用，等待 Windows 客户端恢复连接。

## 一、项目能做什么

### 1. 聊天和长期记忆

- 普通文本聊天。
- 保存对话中的重要事实、用户偏好、术语、风格示例、目标和待解决事项。
- 使用关键词检索、全文检索和向量检索召回相关内容。
- 通过摘要整合、事实提取、反思和遗忘策略，避免所有历史消息无限增长。
- 对主人和其他 QQ 用户按身份隔离记忆。

### 2. 知识库和文档

- 上传或导入 Markdown、文本、CSV、JSON、DOCX、PDF 等文档。
- 文档会被切块，之后可以在聊天中检索和问答。
- 支持按文档域、项目和小说实体进行检索，减少不同资料之间的串库。
- 可以通过“写文档”类命令生成 Markdown 文档，并同步到知识库。
- 可以把简历资料放入知识库，再请求优化并导出 Word 文件。

### 3. 图片识别

- 浏览器、Windows 桌面端和 QQ 私聊都可以发送图片。
- 支持 JPEG、PNG、WebP。
- 服务端默认限制单张图片不超过 10 MB，并同时检查文件头和 MIME 类型。
- 原始图片只在当前请求中处理，不把原始图片字节写入记忆库。
- 图片请求与普通文字聊天使用独立的视觉模型配置。

### 4. 提醒、目标和工作日志

- 中文自然语言提醒，例如“30分钟后提醒我喝水”“明早9点提醒我开会”。
- 通过“目标：……”创建目标，通过“目标进度：……”更新进度。
- 通过“记录：……”记录工作日志。
- 到期提醒可以通过 QQ 推送给主人。
- 每日小结、每周反思和画像刷新由服务器定时任务完成。

### 5. Windows 桌面助手

- PySide6 悬浮机器人和聊天气泡面板。
- 支持拖动、缩放、最大化、置顶、托盘菜单和状态灯。
- 支持选择图片、粘贴剪贴板图片和只发送图片。
- 快捷启动器可以注册网页、应用、搜索模板和指定浏览器。
- 本地执行器支持打开、列目录、读文件、复制、备份、移动、重命名和搜索文件。
- 文件操作受白名单和确认层限制，不提供任意远程 Shell 执行。
- 小说工作台入口可以自动建立 SSH 隧道。

### 6. 健身私教模块

健身模块以本地 SQLite 为主数据源，在保留旧版体重/自由文本训练台账的基础上，提供结构化动作库、训练计划、训练会话、训练组、身体指标和训练统计。

当前支持：

- 动作目录：内置少量常用动作，可从本地 JSON 导入 Free Exercise DB 等公开数据；导入时保留来源、许可证和署名元数据。
- 训练计划：创建、查看、激活和归档计划；同一用户同时只保留一个激活计划。
- 训练记录：开始训练、记录重量/次数/RPE/RIR、完成训练；外部导入预留幂等字段，但本轮不连接第三方账号。
- 分析：训练次数、完成率、训练组数、总训练量、肌群容量、估算 1RM、近期高重量动作和体重趋势。
- AI 私教：`POST /api/fitness/plans/generate` 只生成经过校验的计划草案，不会自动写入；保存和激活必须由用户明确确认。

聊天入口兼容以下命令：

```text
记录体重：70.5
训练记录：深蹲 60kg 5x5
开始今天训练
记录组：卧推 60kg x 8 RIR2
完成今天训练
健身进度
当前训练计划
```

结构化接口统一位于 `/api/fitness/*`，只允许 `owner` 或 `internal` 角色；collector、executor 和访客不能访问。MCP 健身工具默认随 MCP 总开关关闭，读取工具受身份限制，写入工具还必须传入显式确认。动作 JSON 导入命令示例：

```bash
python server/scripts/import_fitness_catalog.py exercises.json \\
  --source free-exercise-db --license Unlicense --attribution "数据源署名"
```

本轮只实现本地优先核心和标准化导入边界，不复制 SparkyFitness、GymCoach、LiftTrace 或 wger 的完整应用，不自动下载第三方数据，也不实现 Hevy/wger 双向同步。外部项目的代码、数据和许可证必须在实际部署前单独审核。

### 7. QQ 接入

通过 NapCat、AstrBot 和本项目插件，可以在手机 QQ 私聊中使用助手：

- 主人使用主人角色权限。
- 访客可以私聊，但记忆按 QQ 号隔离。
- 群聊默认一律静默，不上传群聊消息。
- 主人可以发送文档入知识库。
- 主人和访客使用不同的 token；访客请求还需要 QQ 身份 HMAC 签名。
- 详细配置见 [QQ 接入运维手册](docs/QQ_OPS.md)。

### 8. 小说工作台

当前小说工作台已经支持：

- 创建、改名和删除小说项目。
- 创建、编辑和查看章节。
- 草稿、已发布和存档等章节状态。
- 创建小说生成任务，查看生成进度和失败原因。
- 生成结果先进入待确认状态，确认后才发布，不会直接覆盖已发布正文。
- 章节全文搜索和索引重建。
- 项目成员和项目级权限。
- 生成任务重试、取消、心跳、重启恢复和审计记录。
- 将已发布章节安全同步到项目文件目录。

当前**没有**完整集成外部 `oh-story-claudecode` Skill 包，也没有在工作台中实现完整的扫榜、平台爬取、自动拆文、总纲/卷纲/细纲生成流水线。`server/app/novel/outline.py` 目前主要是为后续接入保留接口的占位读取层。不要根据项目名称或旧讨论把尚未实现的能力当成现成功能。

### 9. 可选 MCP 接口

项目包含一个默认关闭的 MCP stdio 服务，可供支持 MCP 的本地 Agent 调用记忆、知识库、任务和小说工具。它是独立进程，不随普通 FastAPI 请求自动开放；启用时仍按 `owner` 或 `internal` 角色限制权限，并保留审计记录。

MCP 不是 QQ 或手机入口，也不负责图片上传；默认不提供任意 Shell、Python 或删除工具。第一次部署不需要开启它，确认 API、Web 和权限边界稳定后再单独配置。

## 二、系统结构

```text
                       手机浏览器 / QQ
                              |
                              v
+---------------------------------------------------------------+
|                         服务器                               |
|                                                               |
|  FastAPI + SQLite + 定时任务 + LLM/Embedding                 |
|  聊天、记忆、知识库、图片识别、提醒、小说工作台和生成任务     |
|                                                               |
+--------------------------+------------------------------------+
                           |
              私有网络或 SSH 隧道
                           |
        +------------------+------------------+
        |                                     |
        v                                     v
+-------------------+                 +----------------------+
| Windows 采集器    |                 | Windows 桌面机器人   |
| 窗口/浏览器/Git   |                 | 悬浮球、聊天、托盘   |
| 心跳、脱敏、缓存  |                 | 本地执行器、图片     |
+-------------------+                 +----------------------+

QQ 通道：NapCat -> AstrBot -> QQ 插件 -> 服务器 API
```

### 一次普通聊天请求

```text
用户输入文字或图片
        |
        v
API 鉴权、身份确认、请求幂等检查
        |
        +--> 命中快捷命令：直接执行，通常不调用 LLM
        |
        +--> 未命中：检索记忆和知识库，组装上下文
                         |
                         v
                    调用 LLM
                         |
                         v
          返回回复、记录消息、更新用量和命中反馈
```

### 服务器和 Windows 的职责边界

| 能力 | 服务器在线即可 | 需要 Windows 在线 |
|---|---:|---:|
| 普通聊天 | 是 | 否 |
| 记忆和知识库问答 | 是 | 否 |
| 图片识别 | 是 | 否，QQ/浏览器可直接使用 |
| 提醒、每日小结、周报 | 是 | 否 |
| 小说项目、章节和生成任务 | 是 | 否 |
| 读取 Windows 本地文件 | 否 | 是 |
| 打开 Windows 应用或网页 | 否 | 是 |
| Windows 窗口、浏览器、Git 采集 | 否 | 是，且默认关闭 |
| 桌面悬浮机器人 | 否 | 是 |

## 三、目录结构

```text
personal-ai-assistant/
├── server/                 # FastAPI 服务端、数据库、定时任务和 Web 页面
│   ├── run.py              # 服务端启动入口
│   ├── app/api/            # HTTP API
│   ├── app/chat/           # 聊天上下文、路由、检索和生成编排
│   ├── app/core/           # LLM、Embedding、记忆、知识库和调度基础设施
│   ├── app/novel/          # 小说项目、章节、生成任务和工作流
│   ├── app/services/       # 记忆、反思、提醒、文档、小说等业务服务
│   ├── app/web/static/     # 聊天页和小说工作台前端
│   └── tests/              # 服务端隔离测试
├── collector/              # Windows 行为采集器和远程执行器客户端
├── desktop/                # Windows PySide6 桌面机器人
├── qq/astrbot_plugin_xy/  # AstrBot QQ 插件
├── common/                 # 跨端共享的脱敏、文件操作和启动器逻辑
├── scripts/                # 部署、开机自启、打包和导入脚本
├── docs/                   # 部署、运维、QQ、API 和设计文档
├── .env.example            # 脱敏配置模板
├── Makefile                # 常用开发命令
└── pyproject.toml          # ruff 和 pytest 的仓库级配置
```

## 四、开始之前要准备什么

### 必需准备

1. 一个 Linux 服务器，推荐使用 Ubuntu 或 Debian。
2. Python 3.12 或兼容版本。仓库 CI 以 Python 3.12 为基准。
3. Git。
4. 一个 OpenAI 兼容格式的 LLM 服务地址和至少一个 API Key。
5. 如果要使用完整知识库向量检索，再准备 Embedding 服务和 API Key。
6. 一种安全网络方式，让你能访问服务器，例如 Tailscale 或 SSH 隧道。

### 可选准备

- Windows 10/11 和 Python：用于桌面机器人、采集器和本地执行器。
- NapCat 和 AstrBot：用于 QQ 私聊入口。
- Node.js：用于检查 Web 前端 JavaScript 语法。
- Chromium 浏览器：只有未来需要浏览器自动化或榜单采集时才需要；当前项目没有集成完整扫榜功能。

### 不要提前准备或提交到 GitHub 的内容

以下内容只放在本地配置或服务器私有目录，不要写进 README、源码、Issue 或提交记录：

- LLM、Embedding、API、QQ、NapCat、SSH 的 Token 和 Key。
- HMAC secret、密码、Cookie、浏览器登录态。
- 真实 QQ 号。
- 公网 IP、私网 IP、服务器主机名和本机绝对路径。
- 小说正文、私人文档、聊天数据库、行为采集数据和日志。

## 五、第一次部署：先启动服务器

下面的示例使用占位符。把 `<your-github-username>`、`<your-repository>`、`<server-project-path>` 等替换成你自己的值；不要把真实密钥替换回 README。

### 1. 从 GitHub 获取代码

在服务器上执行：

```bash
git clone https://github.com/<your-github-username>/<your-repository>.git <server-project-path>
cd <server-project-path>
```

如果仓库是私有仓库，请先在服务器上准备好 GitHub SSH 访问权限或其他安全的 Git 凭据。不要把 GitHub Token 写进命令、脚本或文档。

### 2. 创建 Python 虚拟环境并安装服务端依赖

```bash
cd <server-project-path>
python3 -m venv server/.venv
server/.venv/bin/python -m pip install --upgrade pip
server/.venv/bin/python -m pip install -r server/requirements.txt
```

虚拟环境的作用是把项目依赖和系统 Python 隔离开。以后升级依赖，也只操作这个虚拟环境。

### 3. 创建本地配置文件

```bash
cd <server-project-path>
cp .env.example .env
chmod 600 .env
```

然后编辑 `.env`。`.env` 已被 `.gitignore` 排除，不应提交。

至少确认以下配置：

```dotenv
HOST=0.0.0.0
PORT=8000

# OpenAI 兼容格式的模型服务
LLM_BASE_URL=https://<your-llm-provider>/v1
LLM_API_KEYS=<your-llm-key>
LLM_MODEL=<your-chat-model>

# 完整知识库检索建议配置
EMBEDDING_BASE_URL=https://<your-embedding-provider>/v4
EMBEDDING_API_KEY=<your-embedding-key>
EMBEDDING_MODEL=<your-embedding-model>
EMBEDDING_DIMENSION=<your-embedding-dimension>

# 生产环境至少配置一个长度足够且随机的 API token
API_TOKEN=<your-random-api-token>

# 数据目录可以先使用模板默认值
DB_PATH=./data/assistant.db
NOVEL_ROOT=./data/novels
```

生成随机 API Token 的一种方式：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

把命令输出复制到本地 `.env`，不要复制到 README 或终端截图中。生产环境不能使用空 token、短 token 或示例占位符。

### 4. 启动服务端

第一次建议前台启动，方便直接看到错误：

```bash
cd <server-project-path>/server
.venv/bin/python run.py
```

看到 uvicorn 开始监听后，另开一个终端做健康检查：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/ready
```

`/api/health` 只检查进程是否存活；`/api/ready` 会检查数据库、定时任务、LLM 配置和向量能力。向量能力不可用时可能显示降级，但关键词检索仍可能工作；LLM 和数据库失败则需要先修复配置。

### 5. 后台运行

确认前台运行正常后，可以暂时使用：

```bash
cd <server-project-path>/server
nohup .venv/bin/python run.py > <server-log-path> 2>&1 &
echo $! > <server-pid-path>
```

长期运行建议使用经过检查的 systemd 或其他进程管理器。仓库中的 `scripts/deploy_server.sh` 是 Ubuntu/Debian 示例模板，不代表每一台服务器当前都已经由 systemd 接管。使用前请检查仓库地址、服务用户、目录权限和 `.env` 位置。

详细运维流程见 [服务端说明](server/README.md)、[部署环境说明](docs/DEPLOYMENT.md) 和 [运维手册](docs/OPS.md)。文档中的真实实例路径只适合维护者自己的服务器，不要照抄到公开仓库。

## 六、打开 Web 聊天页和小说工作台

服务端启动后提供两个主要页面：

```text
http://<server-private-address>:8000/
http://<server-private-address>:8000/novel/
```

如果服务器只允许私有网络访问，请先让电脑或手机加入同一个私有网络。不要为了方便把 8000 端口直接暴露到公网。

第一次访问页面时：

1. 打开页面右下角或顶栏的 Token 设置。
2. 输入服务器 `.env` 中对应的主人 token。
3. Token 会保存到当前浏览器的本地存储中，不会追加到网页 URL。
4. 如果返回 401，通常是 Token 缺失或错误；返回 403，通常是角色没有访问该接口的权限。

### 聊天页

聊天页支持：

- 普通对话。
- 历史消息查看和搜索。
- SSE 流式回复、停止和重试。
- 图片选择和图片提问。
- 行为统计、每日小结和周报抽屉。
- 明暗主题切换。

### 小说工作台

小说工作台的常用流程：

1. 点击“新建”，创建一个小说项目。
2. 进入项目后创建章节，填写章节号、标题和正文或草稿。
3. 需要 AI 续写时创建生成任务，填写章节号和写作提示。
4. 等待任务进入“待确认”状态。
5. 阅读草稿和审查结果，确认后再发布。
6. 通过章节全文搜索查找内容，索引异常时手动重建索引。

生成任务的典型状态是：

```text
排队中 -> 生成中 -> 审阅中 -> 待确认 -> 已发布
                         +-> 失败 -> 可重试
```

任务不会自动覆盖已发布正文。项目和章节使用版本号，多个页面同时编辑时可能返回版本冲突，此时应重新加载后再保存。

## 七、安装 Windows 采集器（可选）

采集器不是使用服务器聊天的必要条件。它只在你希望助手知道部分 Windows 工作状态、接收心跳或执行本地白名单指令时才需要。

### 1. 创建环境并安装依赖

在 Windows PowerShell 中执行：

```powershell
cd <project-root>
py -3.12 -m venv collector\.venv
.\collector\.venv\Scripts\python.exe -m pip install -r collector\requirements.txt
```

### 2. 配置服务器地址和 Token

在 Windows 这份工作区的本地 `.env` 中填写：

```dotenv
SERVER_URL=http://<server-private-address>:8000
API_TOKEN=<collector-compatible-token>

# 默认关闭。只有明确需要时才打开
COLLECT_WINDOW=false
COLLECT_BROWSER=false
COLLECT_GIT=false

PRIVACY_FILTER=true
GIT_REPOS=<windows-repository-path-1>,<windows-repository-path-2>
```

不要把服务器上的 LLM Key、Embedding Key、QQ secret 或其他无关密钥复制到 Windows `.env`。每台设备只保留它真正需要的配置。

### 3. 前台运行测试

```powershell
cd <project-root>\collector
.venv\Scripts\python.exe main.py
```

默认情况下，采集器只保留心跳和执行器能力，不上传窗口、浏览器和 Git 行为。启用采集前请先阅读 [采集器说明](collector/README.md)，确认你接受采集范围和隐私边界。

采集器具备以下保护：

- 事件离开 Windows 前先经过共享脱敏规则。
- 断网时暂存在本地缓存，恢复网络后重试。
- 通过事件幂等键避免重复入库。
- 上报心跳，让服务器知道电脑是否在线。
- 本地执行器只接受服务器白名单指令。

### 4. 设置开机自启

确认前台运行正常后，以管理员 PowerShell 执行：

```powershell
cd <project-root>
.\scripts\install_autostart.ps1
```

脚本会注册采集器和桌面机器人任务。安装脚本会使用当前系统的 Python；如果系统中有多个 Python，请先确认 `Get-Command python` 指向正确环境。

## 八、安装 Windows 桌面机器人（可选）

### 1. 创建环境并安装依赖

```powershell
cd <project-root>
py -3.12 -m venv desktop\.venv
.\desktop\.venv\Scripts\python.exe -m pip install -r desktop\requirements.txt
```

### 2. 配置连接信息

Windows 本地 `.env` 至少需要：

```dotenv
SERVER_URL=http://<server-private-address>:8000
API_TOKEN=<desktop-compatible-token>
```

如果要让“小说工作台”按钮自动建立 SSH 隧道，可以增加：

```dotenv
NOVEL_TUNNEL_TARGET=<ssh-user>@<ssh-host>
NOVEL_TUNNEL_LOCAL_PORT=18000
NOVEL_TUNNEL_REMOTE_HOST=127.0.0.1
NOVEL_TUNNEL_REMOTE_PORT=8000
NOVEL_TUNNEL_IDENTITY_FILE=<optional-private-key-path>
```

更推荐把 SSH 主机别名、端口和私钥配置到 Windows 的 SSH config 中。不要把私钥内容、密码或真实主机信息写进仓库。

### 3. 启动

```powershell
cd <project-root>\desktop
.venv\Scripts\python.exe main.py
```

可以从悬浮球、托盘或聊天面板打开小说工作台。配置 SSH 隧道后，桌面端会先检查本地端口，再打开浏览器；没有配置隧道时会回退到 `SERVER_URL/novel/`。

### 4. 日常操作

- 左键或菜单打开聊天面板。
- 右键悬浮球查看托盘功能。
- 发送图片时可以选择文件或粘贴剪贴板图片。
- 注册快捷启动器时，目标会进入本地白名单；涉及文件或破坏性操作时仍需要确认。
- 机器人异常退出时，查看 `desktop/logs/` 下的日志；不要把日志直接上传到公开 Issue，因为日志可能包含路径或用户输入。

详细说明见 [桌面端说明](desktop/README.md)。

## 九、配置项怎么理解

完整模板见 [.env.example](.env.example)。下面只解释最常见的配置。

| 配置 | 用途 | 是否必需 |
|---|---|---:|
| `HOST` / `PORT` | 服务端监听地址和端口 | 是，有默认值 |
| `LLM_BASE_URL` | OpenAI 兼容模型服务地址 | 是 |
| `LLM_API_KEYS` | 一个或多个 LLM Key | 是，生产环境至少一个 |
| `LLM_MODEL` | 普通聊天模型名 | 是 |
| `VISION_LLM_MODEL` | 图片识别模型名 | 使用图片时需要 |
| `EMBEDDING_BASE_URL` | 向量服务地址 | 使用向量检索时需要 |
| `EMBEDDING_API_KEY` | 向量服务 Key | 使用向量检索时需要 |
| `EMBEDDING_DIMENSION` | 向量维度 | 必须与服务一致 |
| `DB_PATH` | SQLite 数据库位置 | 有默认值 |
| `NOVEL_ROOT` | 小说项目文件根目录 | 有默认值 |
| `API_TOKEN` | 兼容旧客户端的共享 token | 生产环境至少有一个角色 token |
| `OWNER_API_TOKEN` | 主人 Web/内部访问 token | 推荐配置 |
| `COLLECTOR_API_TOKEN` | 采集器角色 token | 分角色部署时使用 |
| `EXECUTOR_API_TOKEN` | Windows 执行器角色 token | 分角色部署时使用 |
| `QQ_API_TOKEN` | QQ 访客入站 token | 使用访客 QQ 时需要 |
| `QQ_IDENTITY_SECRET` | QQ 访客身份签名 secret | 使用访客 QQ 时需要 |
| `QQ_PUSH_URL` | 服务器推送 QQ 提醒的地址 | 使用 QQ 提醒时需要 |
| `QQ_PUSH_TOKEN` | 出站 QQ 推送 token | 使用 QQ 提醒时需要 |
| `QQ_ADMIN_ID` | 接收提醒的主人 QQ 号 | 使用 QQ 提醒时需要 |

### 多 Key 和故障切换

`LLM_API_KEYS` 可以用逗号或换行分隔多个 Key。服务端会在临时失败、超时或 Key 冷却时按策略切换。日志只能记录 Key 数量或脱敏指纹，不应打印 Key 原文。

### 角色 token 的简单理解

- 浏览器聊天页和小说工作台：使用主人 token。
- 采集器上报行为：使用采集器 token。
- Windows 执行器轮询：使用执行器 token。
- QQ 访客：使用 QQ token，并额外提供 HMAC 身份签名。

如果你只是单人本地使用，可以先配置 `API_TOKEN`；如果要把 QQ、采集器和执行器分开，建议为不同角色配置不同 token。

## 十、QQ 接入（可选）

QQ 接入链路如下：

```text
手机 QQ 私聊
    -> NapCat
    -> AstrBot
    -> astrbot_plugin_xy
    -> FastAPI /api/chat 或 /api/chat/vision
```

### 基本步骤

1. 准备 NapCat 和 AstrBot 运行环境。
2. 把 `qq/astrbot_plugin_xy/` 作为 AstrBot 插件使用。
3. 在 AstrBot 配置中填写服务器地址、主人 QQ 号、主人 token、访客 token 和身份 secret。
4. 服务端 `.env` 中配置对应的角色 token 和 `QQ_IDENTITY_SECRET`。
5. 先用主人私聊测试文字，再测试图片和文件。
6. 确认群聊始终静默。

常见配置项：

| 插件配置 | 说明 |
|---|---|
| `api_base` | FastAPI 服务根地址 |
| `owner_qq` | 主人 QQ 号，只填本地配置 |
| `owner_api_token` | 主人 token |
| `api_token` | 访客 QQ token |
| `identity_secret` | 与服务器 `QQ_IDENTITY_SECRET` 一致 |
| `onebot_http` | NapCat onebot HTTP 地址 |
| `onebot_token` | NapCat onebot HTTP token |
| `vision_timeout` | 图片下载和识别超时 |
| `container_path_map` | NapCat 容器路径到宿主路径的映射 |

安全行为：

- 群聊消息和群聊图片在上传前静默。
- 访客只能访问自己的对话范围，不能因为请求体伪造 `user_id` 取得主人权限。
- 主人文件入库只允许主人私聊。
- 图片只接受 JPEG、PNG、WebP，默认 10 MB。
- 入站聊天 token 和出站提醒 token 是两条不同链路，不要混用。

完整排障和升级步骤见 [QQ 接入运维手册](docs/QQ_OPS.md)。

## 十一、常用聊天示例

### 工作日志

```text
记录：下午完成了知识库检索测试
写作记录：第5章 3200字
写作进度
```

### 提醒

```text
30分钟后提醒我喝水
明早9点提醒我开会
今晚8点提醒我查看服务器日志
我的提醒
取消提醒：开会
```

### 目标

```text
目标：完成个人助手部署
目标进度：服务器已经启动，正在配置 QQ
目标完成：完成个人助手部署
```

### 文档和简历

```text
写文档：标题：部署说明，内容：整理本项目的安装和排障步骤
优化简历：目标岗位：运维工程师
```

简历优化前，需要先把简历文档上传到知识库。模型只应改写已有真实信息，不应凭空添加经历、技能或数据。

### 小说辅助

```text
检查设定冲突：这里粘贴新写的正文
分析章节：这里粘贴章节正文
章节存档：第5章 主角在雨夜发现了新的线索（伏笔：黑色印记、旧地图）
续写：主角站在门口，听见屋内传来第二个脚步声
```

长正文和正式工作流建议使用小说工作台，而不是把整本书一次性粘贴到聊天框。

## 十二、小说数据和工作流

### 数据保存在哪里

小说项目有两部分数据：

1. SQLite 中的项目、章节、生成任务、项目成员、索引和审计记录。
2. `NOVEL_ROOT` 下的项目文件，例如已发布章节和后续扩展的设定/大纲文件。

`NOVEL_ROOT` 是安全边界：项目根目录必须位于该目录内，文件扩展名和大小也有限制。不要把 `NOVEL_ROOT` 指向整个用户家目录、系统目录或 Git 仓库根目录。

### 生成任务为什么不是立即返回正文

小说生成可能需要较长时间，因此工作台会：

1. 创建一个带幂等键的任务。
2. 由服务器调度器定期认领任务。
3. 记录生成、审阅、失败和重试状态。
4. 把结果放入草稿和待确认状态。
5. 用户确认后才发布并同步文件。

服务器重启后，过期的运行中任务会被恢复；失败任务可以在工作台重试。若任务长时间停在排队中，先检查服务器是否在线、LLM 是否就绪和定时任务是否运行。

### 当前明确限制

以下能力目前不要当成已经完成的功能：

- 多平台榜单自动抓取。
- 需要登录态的浏览器 CDP 扫榜。
- 完整的榜单趋势分析。
- 自动导入整本小说并逆向生成完整设定。
- 完整的总纲、卷纲、细纲编辑器。
- 外部 `oh-story-claudecode` Skill 包的服务器端运行。

这些能力以后如果要做，应先设计独立任务、权限、存储和恢复机制，不能直接把外部 CLI、Hook 或 Agent 文件当成 Web 后端使用。

## 十三、定时任务

服务器启动后会注册定时任务。默认时间按项目配置使用的时区执行，常见任务包括：

| 任务 | 默认频率或时间 | 作用 |
|---|---|---|
| 摘要整合 | 每 4 小时 | 把近期碎片整理成摘要、主题和事实 |
| 记忆淘汰 | 每 6 小时 | 清理过期噪声和低价值内容 |
| 聊天幂等清理 | 每 6 小时 | 清理过期请求记录 |
| 数据备份 | 每日凌晨 | SQLite 热备份和滚动保留 |
| 文档进度同步 | 每日凌晨后 | 将项目文档同步到知识库 |
| 画像刷新 | 每日一次、周报前再刷新 | 更新用户画像 |
| 每日小结 | 每日晚上 | 生成当天总结 |
| 主动开口 | 默认关闭 | 根据开关决定是否推送主动消息 |
| 周报 | 每周一次 | 生成学习和工作反思 |
| QQ 提醒推送 | 每分钟检查 | 推送到期提醒 |
| 小说生成 | 每分钟最多处理一个 | 执行排队中的小说生成任务 |

定时任务失败时会记录日志；如果配置了 QQ 推送，部分失败会向主人发送告警。

## 十四、隐私和安全

### 配置和数据

- `.env`、数据库、小说正文、日志、采集缓存和备份不提交 GitHub。
- 示例配置只使用 `<your-...>` 形式的占位符。
- 不要在 Issue、截图和日志中公开 Token、Key、QQ 号、Cookie、HMAC secret 或服务器地址。
- 生产环境至少配置一个随机且足够长的 API token。
- 不要把服务端 `.env` 原样复制到 Windows 或 QQ 宿主；每个组件只配置自己需要的字段。

### 网络

- 推荐让 API 只通过 Tailscale、VPN 或 SSH 隧道访问。
- 不建议把 8000 端口直接暴露到公网。
- SSH 使用密钥认证，并按服务器安全策略限制登录。
- 浏览器 Token 存在本地浏览器存储中；共用电脑使用完应清除。

### 采集

- Windows 窗口、浏览器和 Git 采集默认关闭。
- 开启采集前，先检查 `.env` 中的 `COLLECT_WINDOW`、`COLLECT_BROWSER` 和 `COLLECT_GIT`。
- 事件在 Windows 本地脱敏后再上传。
- 浏览器采集不应被理解为上传网页正文；具体采集字段以 `collector/` 实现为准。
- 断网缓存也属于个人数据，不要提交或上传到公共位置。

### QQ

- 群聊默认静默。
- 主人和访客按 token、HMAC 和 QQ 号分流。
- 服务端不信任请求体单独提交的身份字段。
- 图片原始字节不进入长期记忆。

### 本地执行器

- 服务器端会检查动作类型和路径白名单。
- Windows 端只允许白名单根目录中的文件操作。
- 打开应用和破坏性操作需要额外确认。
- 不提供任意 Shell 或远程脚本执行。

## 十五、备份、恢复和数据迁移

建议至少备份两类数据：

1. 服务端 SQLite 数据库。
2. `NOVEL_ROOT` 下的小说项目目录。

升级或迁移前：

```text
1. 停止服务端，避免 SQLite 正在写入。
2. 复制数据库和小说目录到受保护的备份位置。
3. 记录当前 Git 提交号和配置变更，但不要备份到公开仓库。
4. 更新代码并运行健康检查。
5. 确认聊天、项目列表和章节内容正常后，再删除旧实例。
```

不要把数据库、小说目录或 `.env` 放在 GitHub 作为“同步方案”。Windows 端的代码更新统一来自 GitHub；个人数据通过服务器 API、项目文件同步或专用备份流程管理。

## 十六、更新代码

### Windows 端

Windows 端必须从 GitHub 更新，不要从服务器工作区直接复制代码：

```powershell
cd <project-root>
git pull --ff-only origin main
```

如果工作区有本地修改，先保存或提交到自己的分支，再执行拉取。不要用强制覆盖命令处理冲突。

### 服务器端

```bash
cd <server-project-path>
git pull --ff-only origin main
```

拉取后需要重启正在运行的服务进程，否则旧进程仍会继续使用旧代码。重启方式以当前实例的进程管理方式为准，不要假设所有服务器都有同名 systemd 服务。

更新后建议检查：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/ready
```

如果改动了 Web JavaScript 或 CSS，还要确认页面引用的版本参数已经更新，避免浏览器继续使用旧缓存。

## 十七、测试和开发

### 服务端测试

```bash
cd <project-root>
server/.venv/bin/python -m pytest server/tests/ -q
```

服务端测试使用隔离数据库；不要把测试指向真实生产数据库。

### QQ 插件测试

在具备测试依赖的环境中执行：

```bash
python -m pytest qq/astrbot_plugin_xy/test_main.py -q
```

测试使用 AstrBot 和 HTTP 桩，不应发送真实 QQ 消息。

### Windows 桌面端测试

```powershell
cd <project-root>
.\desktop\.venv\Scripts\python.exe -m pytest desktop\tests\ -q
```

### 静态检查

```bash
cd <project-root>
server/.venv/bin/python -m pip install ruff
server/.venv/bin/ruff check .
node --check server/app/web/static/chat.js
node --check server/app/web/static/novel/index.js
bash -n scripts/deploy_server.sh
```

GitHub Actions 会在推送和 Pull Request 时运行服务端、QQ、桌面端、ruff、JavaScript 和 shell 检查。测试结果应以当前 CI 页面和本地实际输出为准，不要把旧的测试数量写死在文档中。

## 十八、常见问题

### 1. 浏览器打开页面返回 401

检查：

- 是否在页面 Token 弹窗中填写了 token。
- token 是否来自当前服务器的 `.env`。
- 是否把访客 QQ token 当成了主人 Web token。
- 浏览器是否保存了旧 token；必要时清除站点本地存储后重新填写。

### 2. `/api/ready` 返回 503

先执行 `/api/health` 确认进程还活着，再查看服务端日志。常见原因包括：

- LLM Key 没有配置或仍是模板值。
- 模型名为空。
- 数据库无法打开或迁移失败。
- 定时任务没有正常启动。
- 生产环境 token 不符合长度要求。

### 3. Windows 机器人显示离线

检查：

- Windows 是否加入与服务器相同的私有网络。
- `SERVER_URL` 是否正确。
- Token 是否匹配。
- 服务端 `/api/health` 是否正常。
- `desktop/logs/desktop.log` 和 `faulthandler.log` 是否有错误。

### 4. 采集器没有数据

这可能是正常的，因为三个采集开关默认都是关闭的。先检查：

```dotenv
COLLECT_WINDOW=true
COLLECT_BROWSER=true
COLLECT_GIT=true
```

只打开你确实需要的通道，并确认 `GIT_REPOS` 使用的是 Windows 本地路径。再查看采集器日志和任务计划状态。

### 5. QQ 私聊没有回复

检查 `api_base`、主人/访客 token、`identity_secret` 和服务端健康状态。主人请求和访客请求使用不同权限；不要把出站 `QQ_PUSH_TOKEN` 填到入站聊天 token 中。

### 6. 群聊中机器人没有回复

这是预期行为。项目设计为群聊静默，避免把个人记忆或私人信息带入群聊。

### 7. 图片识别失败

检查：

- 是否是 JPEG、PNG 或 WebP。
- 是否超过默认 10 MB。
- `VISION_LLM_MODEL` 和视觉 API Key 是否配置。
- QQ 入口是否能通过 NapCat 取到图片。
- 图片请求是否使用了 `/api/chat/vision`，而不是普通 JSON `/api/chat`。

### 8. 小说任务一直排队

检查服务端是否正常运行、调度器是否启动、LLM 是否就绪，以及任务是否因版本冲突或失败进入错误状态。小说生成任务不是即时同步接口，默认由调度器每分钟处理。

### 9. 页面显示旧版本

先强制刷新浏览器。如果仍然是旧页面，检查前端 HTML 中的 `?v=` 资源版本是否随 JS/CSS 修改同步增加，然后重启服务端进程。

### 10. 想把小说或数据库放进 GitHub

不要这样做。GitHub 只同步代码和脱敏文档；小说正文、数据库、日志、备份和密钥应留在服务器私有存储中。

## 十九、进一步阅读

| 文档 | 适合什么时候看 |
|---|---|
| [.env.example](.env.example) | 第一次配置环境变量 |
| [服务端说明](server/README.md) | 了解 FastAPI、API 和服务端启动方式 |
| [采集器说明](collector/README.md) | 配置 Windows 行为采集和隐私过滤 |
| [桌面端说明](desktop/README.md) | 配置悬浮机器人、图片和 SSH 隧道 |
| [QQ 接入运维手册](docs/QQ_OPS.md) | 配置 NapCat、AstrBot 和 QQ 鉴权 |
| [小说 API 契约](docs/novel-api-contract.md) | 对接小说项目、章节和生成任务 API |
| [运维手册](docs/OPS.md) | 启停服务、查日志和排障 |
| [部署环境说明](docs/DEPLOYMENT.md) | 了解服务器部署边界和安全注意事项 |
| [测试指南](docs/TESTING_GUIDE.md) | 编写和运行测试 |
| [经验教训](docs/LESSONS.md) | 查看历史问题复盘和工程约束 |
| [架构对比](docs/ARCHITECTURE_COMPARISON.md) | 了解设计取舍 |
| [许可证](LICENSE) | 查看使用和分发限制 |

## 二十、当前路线和已知边界

已经具备的基础能力：

- 服务端聊天、记忆、知识库和图片识别。
- Web 聊天页和小说工作台。
- Windows 桌面端、采集器和本地执行器。
- QQ 私聊、图片和主人文档入库。
- API 角色鉴权、QQ 身份签名、请求幂等和定时任务。
- 小说项目、章节、生成任务、审阅、发布、索引和审计基础设施。

仍需要单独设计或验证的方向：

- 更完整的 Web 仪表盘和运营视图。
- 更丰富的小说大纲、设定和细纲编辑器。
- 榜单采集、平台登录态和浏览器自动化。
- 更复杂的多 Agent 写作流程。
- 生产环境的标准化进程管理和灾备演练。

外部小说 Skill 包不属于当前仓库的运行依赖。本项目不会因为 README 中提到某个外部项目，就自动获得该项目的功能。

## 许可证

这是一个个人项目，许可证和使用限制见 [LICENSE](LICENSE)。除非获得明确许可，不要复制、分发、公开部署或将其用于商业用途。第三方依赖遵循各自许可证。

## 贡献和安全报告

提交代码前请：

1. 运行相关测试和静态检查。
2. 检查 `git diff` 中没有 `.env`、日志、数据库、小说正文、Token、Key、QQ 号、私网地址或个人路径。
3. 不要在 Issue 中公开敏感配置或原始日志。
4. Windows 和服务器都通过 GitHub 更新，不要直接互相复制工作区文件。
