# Server —— 服务端（JD 服务器）

FastAPI 单进程服务，承担聊天编排、记忆闭环、行为事件接收、统计聚合、画像更新、周报生成，以及一期图片识别。当前实例目录为 `/root/personal-ai-assistant/server`，端口 8000，手工进程日志为 `/tmp/assistant.log`。

## 目录结构

```text
server/
├── run.py                  # 启动入口
├── app/
│   ├── main.py             # FastAPI 实例 + 路由注册 + 启动钩子 + 鉴权中间件
│   ├── config.py           # 配置加载（.env / 环境变量）
│   ├── api/
│   │   ├── chat.py         # /api/chat 与 /api/chat/vision
│   │   ├── events.py       # 行为事件接收（采集器推送）
│   │   ├── stats.py        # 行为统计查询
│   │   └── reports.py      # 周报查询/手动触发
│   ├── chat/
│   │   ├── context.py      # 请求模型、身份、request_id 幂等
│   │   ├── routing.py      # 零 LLM 命令路由
│   │   ├── retrieval.py    # 记忆/知识检索
│   │   ├── prompting.py    # 普通/多模态 prompt 组装
│   │   └── pipeline.py     # 普通聊天与视觉模型编排
│   ├── core/
│   │   ├── llm.py          # OpenAI 兼容客户端、多 Key 故障切换、用量记账
│   │   ├── embedding.py    # Embedding 客户端
│   │   ├── memory.py       # 记忆写入/检索/注入
│   │   └── scheduler.py    # APScheduler 定时任务
│   ├── services/
│   │   ├── vision.py       # JPEG/PNG/WebP、MIME、大小和文件头校验
│   │   ├── request_dedup.py # 数据库级请求幂等
│   │   └── ...
│   ├── models/
│   │   └── database.py     # SQLite 建表 + sqlite-vec 接入
│   └── web/                # 前端页面（聊天/仪表盘）
├── tests/                  # pytest 隔离回归与专项边界测试
└── requirements.txt
```

## HTTP 路由

| 路由 | 载荷/用途 |
|---|---|
| `POST /api/chat` | 普通聊天 JSON：`message`、可选 `request_id`/`user_id`；图片请求不能塞进此接口 |
| `POST /api/chat/vision` | 图片识别 multipart：必填 `image`、`request_id`，可选 `message`/`user_id`；只接受 JPEG/PNG/WebP，默认 10MB |
| `GET /api/health` | 轻量存活探针，公开且无副作用 |
| `GET /api/ready` | 数据库、调度器、LLM 配置、向量能力就绪检查，公开且无副作用 |
| `GET /api/messages` | 主人消息历史 |
| 其他 `/api/*` | 行为、统计、报告、知识库、执行器、小说等业务接口，按角色 token 限制 |

### `/api/chat/vision` 处理边界

1. API 层以 `limit + 1` 方式读取上传内容，校验文件大小、声明 MIME、文件头和格式完整性。
2. 合法图片只在当前请求内存中生成 data URL，不抓取远程 URL，也不把原始图片字节落库。
3. 图片请求跳过零 LLM 快捷命令，组装 OpenAI 兼容的多模态 `content`，使用 `VISION_LLM_MODEL`。
4. 用户记忆只保存文字与 `[图片]` 标记；`request_id` 的请求摘要包含图片 SHA-256、MIME 和大小。
5. 视觉上游失败返回友好 `ChatResponse`，并标记为可重试；该失败不会写入成功幂等缓存。

### 鉴权与幂等

- 配置服务端 token 后，`/api/chat/vision` 允许 `owner`、`internal`、`qq` 角色；`collector`/`executor` 角色被拒绝。
- QQ 角色除 `QQ_API_TOKEN` 外，还必须通过 `QQ_IDENTITY_SECRET` HMAC 校验 QQ 号、时间戳和 `request_id`；body 的 `user_id` 只做一致性校验。
- 同一用户同一 `request_id` 且请求内容（文字 + 图片 SHA-256）相同，复用成功响应；改内容返回 409，处理中返回 409 并带 `Retry-After`。
- 输入边界错误使用 400/413/415，鉴权错误使用 401/403；视觉上游超时不把错误文本当作正常 assistant 记忆。

## 配置

普通聊天与图片识别必须分别配置：

```dotenv
LLM_API_KEYS=<key-1>,<key-2>
LLM_MODEL=deepseek-v4-flash
VISION_LLM_MODEL=deepseek-v4-flash-vision-exp
VISION_MAX_IMAGE_BYTES=10485760
VISION_TIMEOUT=90

# 按角色拆分时，QQ 插件使用这一项
QQ_API_TOKEN=<qq-api-token>
QQ_IDENTITY_SECRET=<shared-hmac-secret>
QQ_IDENTITY_MAX_AGE_SECONDS=300
```

`LLM_API_KEYS` 支持逗号或换行分隔，留空时兼容 `LLM_API_KEY`；服务端最多支持 8 个 Key。真实 Key/token/secret 只放 `.env`，日志只允许输出数量或脱敏指纹。

## 启动

```bash
pip install -r requirements.txt
cp ../.env.example ../.env   # 填脱敏配置对应的真实值
nohup .venv/bin/python run.py > /tmp/assistant.log 2>&1 &
```

当前实例不是 systemd 管理；新装 systemd 模板在仓库根目录 `scripts/deploy_server.sh`，具体启停、日志和无副作用检查见 `docs/OPS.md`。

## 实现状态

- [x] 普通聊天：命令短路、记忆/知识检索、上下文注入、LLM 回复和用量记账
- [x] `POST /api/chat/vision`：multipart 图片 + 文字、图片-only、MIME/文件头/10MB 校验
- [x] 视觉模型与普通聊天模型分离：`deepseek-v4-flash-vision-exp` / `deepseek-v4-flash`
- [x] 多 Key 故障切换、超时、请求幂等和可重试失败释放
- [x] API 角色鉴权、QQ 身份 HMAC、数据库隔离与 MCP 独立 stdio 权限边界
- [x] 行为事件接收 + 统计 API
- [x] 周报服务 + Web 静态界面 + 定时任务

## 测试证据

- 服务端隔离回归：**1004 passed / 2 skipped**；视觉用例包含在该回归中。
- 不为健康检查或文档同步重复调用真实视觉服务。
