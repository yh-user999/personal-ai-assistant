# Personal AI Assistant 🤖

个人智能助手 —— Windows 本地 + 服务器混合部署，桌面悬浮机器人形态，实现「记忆 → 分析 → 学习」闭环的个人工作助手。

> 状态：六课带教全部完成 · 进阶课 9/10（第 6 课 CI / 第 7 课仪表盘待做）· **服务端隔离回归 1004 passed / 2 skipped** · **QQ 图片专项 5 passed** · **桌面图片专项 6 passed** · QQ 接入已上线 · 图片识别一期已接入

## 能力总览

| 维度 | 能力 |
|------|------|
| **记忆** | 双层记忆（情景原文 + 三元组事实）、向量检索 + BM25 混合、遗忘衰减、术语词典、四维画像 |
| **反思** | 自省（纠正→教训→去重永久遵守，身份类优先）、每日小结（22:00）、每周学习反思（周日 21:00） |
| **拟人** | 自我状态（熟络度/久别/刚被纠正）、隔日情绪跟进（记得你昨天不顺）、主观时间（按事件而非日期回忆）、不确定就直说、主动开口通道（默认关闭，每日 1 条 + 夜间静默 + 无回应降频） |
| **成长感知** | 你说"感觉没收获"时给**事实反证**而非鸡汤：聚合 git 提交/应用时长/话题演进/知识库灌入/纠正次数，指出你忽略的判断价值。零 LLM 聚合 |
| **被动追踪** | 从对话识别目标意向（不需要打命令——`goals` 表长期为空正是因为命令式录入没人用），存为候选，问过两次没回应自动丢弃；知识库主动提示（库里有相关资料但你没问时提一句，带三层冷却防打扰） |
| **人格安全** | 身份守卫：角色扮演/侮辱性命名不进长期人格，改名需显式确认（`kind=identity` 的教训永久最高优先注入，一句玩笑话就能长期扭曲人格） |
| **感知** | 行为实时注入（当前窗口/git 提交/近 1h 活跃）、三通道采集（前台窗口/浏览器历史/git）、心跳健康 |
| **学习** | 关切追踪（在意什么）、风格学习（认可的回复形式）、多轮上下文（8 轮原文 + 摘要续接） |
| **RAG** | 文档知识库：切块 → 向量化 → 混合检索（RRF）→ 带引用回答；Hit@k/MRR 评测体系 |
| **图片识别** | 桌面端选图/剪贴板粘贴/图片-only；QQ 私聊支持主人与访客图片、群聊静默；Web/API 走 `/api/chat/vision` multipart；原始图片不落库 |
| **实体检索** | 小说专名索引 + 五层检索：枚举式提问（"有哪些命丛"）走专名精确匹配而非向量（类名检索精度仅 15.9%，专名接近 100%），注入自带完整度报告 |
| **小说写作** | 设定冲突检查 + 续写辅助 + 写作台账（6.25）；**章节分析二期**：`分析章节：<正文>` 零 LLM 残留检测（AI 元话语/章节尾标记）+ 字数对照 + 1 次 LLM 逻辑/时间线/动机/称谓/设定五维问题清单（带引句与建议）+ 节奏超载评估；`章节存档：第X章 <摘要>` 零 LLM 入库；写第 N 章自动注入前情提要 + 未回收伏笔；生成档长回复自动提炼章节存档（被动抓取，无需打命令） |
| **产出** | 对话式文档生成（"写文档"命令）、简历优化（专家 prompt + .docx 导出）、个性化问候 |
| **交互** | 桌面悬浮机器人（三套自绘皮肤：班德机械版/宇航员重制版/萌系风，呼吸眨眼+状态灯）、气泡聊天（Markdown 渲染 + 面板可缩放/最大化/移动/尺寸记忆）、托盘通知、📌 图钉 |
| **执行** | 快捷启动器（"记住 打开B站=网址"注册常用软件/网页/搜索模板，打开X / 在X搜索 / 指定浏览器 / 时段常用推荐）、文件手（复制/备份/移动/重命名，白名单内）、远程指令队列（原子认领+超时释放）、破坏性操作确认层 |
| **运维** | 10 个定时任务、每日 03:00 热备份（校验 + gzip，3 份日备 + 4 份周备）、行为事件按类留存期淘汰、采集通道停滞告警、开机自启 + 崩溃自愈、黑匣子日志、LLM token 用量记账 |

## 架构

```
┌─ Windows 本地 ──────────────────────────────────────┐
│ ② collector/ 行为采集器（窗口 8s / 浏览器 10min / git 15min）│
│    脱敏 → 攒批 → 幂等推送 → 心跳（5min）                    │
│ ③ desktop/   桌面悬浮机器人（PySide6）                       │
│    文本聊天 / 选图 / 剪贴板粘贴 / 图片-only / 托盘 / 状态灯   │
└────────────────────────────────────────────────────┘
        ↕ 私有加密专线（公网只暴露 SSH 22）
┌─ 云服务器 ──────────────────────────────────────────┐
│ ① server/    FastAPI 单进程（uvicorn）                      │
│    聊天编排 + 记忆闭环 + 知识库 + 行为统计 + 反思生成        │
│    SQLite（WAL）+ sqlite-vec（cosine KNN）                  │
│    普通聊天：deepseek-v4-flash                             │
│    图片识别：deepseek-v4-flash-vision-exp · Embedding：智谱 │
│    /api/chat（JSON）· /api/chat/vision（multipart）          │
└────────────────────────────────────────────────────┘

┌─ QQ 通道 ───────────────────────────────────────────┐
│ NapCat → AstrBot 插件 → /api/chat 或 /api/chat/vision     │
│ 私聊按 QQ 号隔离 + HMAC 身份签名；群聊 stop_event 静默     │
└────────────────────────────────────────────────────┘
```

**一次聊天请求的数据流**：

```
用户消息（桌面端 / QQ 私聊 / Web/API）
  ├─ 图片入口（如有附件）
  │   ├─ 接收 multipart：image + message + request_id（可选 user_id）
  │   ├─ 边界校验 JPEG/PNG/WebP、MIME 与 ≤10MB；服务端不抓取图片 URL
  │   ├─ QQ 额外校验 QQ_API_TOKEN + QQ_IDENTITY_SECRET HMAC；群聊在插件层静默
  │   └─ 组装多模态消息，跳过零 LLM 命令，使用视觉模型
  ├─ 无图片时走身份守卫与命令路由（按注册顺序：身份 → 确认 → 日志 → 时间 → 提醒 → …）
  │   命中即回，零 LLM
  └─ 未命中 → LLM 主路径
       ├─ 检索：记忆（向量+BM25 混合 → 弱命中深挖兜底 → 一跳共现扩散）
       │        知识库（混合检索 + 邻域扩展 + 实体索引五层检索）
       ├─ 注入：稳定档案区（facts/画像/教训/风格/关切/术语/目标/未解决）
       │        动态上下文区（10 轮原文 + 更早摘要 + 记忆 + 知识库
       │                      + 行为 + 情绪 + 自我状态）
       ├─ LLM 生成（普通聊天 deepseek-v4-flash；图片识别 deepseek-v4-flash-vision-exp；usage 记账）
       └─ 写库 + 提升被引用记忆的 importance/hit_count + 后台事实提取
          （图片只保留文本与 [图片] 标记，原始字节不落库）

定时任务（10 个）
  每分钟  QQ 提醒推送（推送成功才消费，失败下轮重推）
  每 4h   摘要整合：碎片 → summary/topics/facts
  每 6h   淘汰：按 kind 分留存期 + 清孤儿向量
  03:00   热备份（integrity_check + gzip，3 日备 + 4 周备）
  04:10   进度同步：docs/*.md 重灌知识库
  04:30   每日画像刷新
  22:00   每日小结  ·  22:10  主动开口（默认关闭）
  周日 20:00 画像 → 21:00 周报
```

**prompt 分区的成本考量**：稳定档案区放在前部，动态上下文区放在末尾。因为 LLM 前缀缓存的命中依赖前缀不变，而缓存读取单价比输入便宜约 30 倍（实测命中率 30%~76%）。**区块顺序是成本决策，不只是可读性。**

## 图片识别一期接口

服务端提供 `POST /api/chat/vision`，使用 `multipart/form-data` 接收图片和文字问题；普通文本仍使用 `POST /api/chat` 的 JSON 接口。

```bash
curl -X POST http://127.0.0.1:8000/api/chat/vision \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -F "image=@sample.png;type=image/png" \
  -F "message=请描述图片中的内容" \
  -F "request_id=desktop-20260905-0001" \
  -F "user_id=10086"
```

| 字段 | 必填 | 说明 |
|---|---:|---|
| `image` | 是 | 图片文件；只接受 JPEG、PNG、WebP，服务端按文件头和 MIME 双重校验 |
| `message` | 否 | 图片问题或补充说明；为空时使用默认识图指令 |
| `request_id` | 是 | 客户端重试幂等键，不能为空，最长 128 个字符 |
| `user_id` | 否 | 用户主体；QQ 请求必须与 HMAC 签名中的 QQ 号一致，主人/内部 token 不信任 body 覆盖身份 |

单图上限由 `VISION_MAX_IMAGE_BYTES` 控制，默认 `10485760` 字节（10MB）。服务端只在请求内存中生成 data URL，不抓取远程图片 URL，也不把原始图片字节写入记忆库。

鉴权规则：配置任意服务端 token 后，必须带 `Authorization: Bearer ...`；该路由仅允许 `owner`、`internal`、`qq` 角色。QQ 插件还必须使用独立的 `QQ_API_TOKEN`，并发送 `X-QQ-User-ID`、`X-QQ-Timestamp`、`X-QQ-Request-ID`（或 `X-Request-ID`）和 `X-QQ-Signature`；签名载荷为“QQ 号、时间戳、request_id”逐行拼接的 HMAC-SHA256，默认 300 秒内有效。

幂等与错误语义：同一用户同一 `request_id` 且消息/图片 SHA-256 相同会复用成功响应；同一 ID 改了消息或图片返回 `409`，请求仍在处理时返回 `409` 并带 `Retry-After: 1`。`400` 表示缺字段、空文件或损坏文件，`401/403` 表示鉴权/身份失败，`413` 表示超过大小上限，`415` 表示格式或 MIME 不支持。视觉上游超时或调用失败返回正常 `ChatResponse` 的友好失败文案，但该失败不会缓存，客户端可用原 `request_id` 重试。

### 图片相关配置

```dotenv
# 普通聊天与图片识别使用不同模型
LLM_MODEL=deepseek-v4-flash
VISION_LLM_MODEL=deepseek-v4-flash-vision-exp
VISION_MAX_IMAGE_BYTES=10485760
VISION_TIMEOUT=90

# 推荐多 Key；留空时回退旧的 LLM_API_KEY
LLM_API_KEYS=<key-1>,<key-2>

# QQ 插件独立鉴权（值只放服务器 .env / AstrBot 配置，不进仓库）
QQ_API_TOKEN=<qq-api-token>
QQ_IDENTITY_SECRET=<shared-hmac-secret>
QQ_IDENTITY_MAX_AGE_SECONDS=300
```

`QQ_API_TOKEN` 只证明请求来自 QQ 插件，`QQ_IDENTITY_SECRET` 才用于证明发送者 QQ 号；AstrBot 插件配置中的 `api_token`、`identity_secret` 分别填入前两项。所有 Key/token/secret 只写脱敏占位符，不要复制真实值到文档或仓库。

## 记忆系统（十通道）

| 通道 | 表 | 注入时机 | 作用 |
|------|-----|----------|------|
| 情境记忆 | `memories` | 向量检索 Top-5 | 相似对话召回 |
| 持久事实 | `facts` | 每次必注入 | 身份/项目/偏好（"我叫小月"） |
| 教训 | `lessons` | 每次必注入 | 用户纠正永久遵守（`UNIQUE(content)` 去重，`kind=identity` 的身份设定优先且不占配额） |
| 画像 | `profile` | 每次必注入 | 四维用户理解 |
| 关切 | `concerns` | 每次必注入 | 在意的话题 + 搁置提醒 |
| 术语 | `jargon_terms` | 命中时注入 | 解释口径一致 |
| 风格范例 | `style_examples` | 每次必注入 | 认可的回复形式 |
| 行为上下文 | `behavior_events` | 每次必注入 | 当前窗口/提交/活跃 |
| 情绪状态 | `mood_log` | 有记录时注入 | 今日情绪曲线 + 负面连击降级 + 隔日跟进 |
| 自我状态 | （只读 `memories`/`lessons`） | 有内容时注入 | 她自己的处境：熟络度/久别/刚被纠正 |

记忆检索的三层增强：

- **主观时间**：注入里的日期换成事件锚点（`[记忆] 接码平台记录那阵子:` 而不是 `2026-08-28:`）。锚点取自已有的 `daily_summaries` / `work_log`，零 LLM；无可用锚点时退回原始日期。人不按日期记事，按事件记事。
- **一跳共现扩散**：捞出"语义不相近但同期出现"的记忆（问跳槽时把当时记的薪资对比也带上）。按数据量门控——带话题的记忆 <200 条时自动跳过，稀疏图上建不出可靠的边。
- **使用反馈**：`memories` / `lessons` 都记 `hit_count`（不衰减的累计注入次数）。它和 `importance` 分工不同——后者会被"你好"这类短句刷高（越短越容易被向量命中），前者才能回答"这条到底被用过没有"。

## 知识库分域检索

**问题**：19 个文档（两本小说 + 项目文档 + PDF 教程 + 简历）混在一张表里检索，跨域污染严重。实测：

| 提问 | 改造前命中 | 改造后 |
|---|---|---|
| 「李羽的能力是什么」 | **6/6 全错**（4 块另一本小说 + 2 块 LESSONS.md）| 跳过检索（facts 已覆盖），1004ms → 108ms |
| 「命丛有哪些」 | 3/6 无关（反代教程 PDF、AI 模板、名词焦虑 PDF）| **6/6** 全是《寂静杀戮》 |
| 「蜃宗为什么挖走命丛」 | 1/6 无关（LESSONS）| **6/6** 全对 |
| 「RAG 检索怎么优化」 | 混检 | 限定项目文档域 |

**根因不是检索算法写错，是 embedding 各向异性**：实测所有块的相似度都塌在 `0.023~0.025` 这个 0.002 宽的区间里（正常应有梯度：相关 0.7+、无关 0.3-）。向量对"相关/无关"没有区分力，`min_similarity=0.35` 这个配置形同虚设——没有任何块能达到。既然向量分不出来，就用**元数据过滤**兜住。

**四层策略**：

```
1. 文档分域          novel(3350) / project_doc(123) / manual(30) / resume(17)
2. 查询意图路由      书名 → 定位到那本书
                    专名（实体表 160 个）→ 定位到那本书
                    体系词（命丛/命图）→ 按出现频次独占度定位（≥90% 才收窄）
                    项目术语/简历/教程 → 对应域
                    facts 已覆盖且知识库无内容 → 明确跳过检索
3. 严格分域 + 兜底   先只搜目标域；无结果再全域（BM25 仅 20ms，多跑一次可接受）
4. 权重与降级        BM25 权重 1.5→3；向量 top-k 相似度极差 <0.005 时
                    本轮放弃向量结果（等于随机噪声，融进 RRF 只会挤掉正确命中）
```

**耗时对比**（实测）：向量 218~1244ms（含 embedding 网络往返）· BM25 8~27ms · 实体索引 621ms · 邻域扩展 1~4ms。向量最贵而质量最差，所以提 BM25 权重同时省了大量耗时。

**自我污染的处理**：`LESSONS.md`／`TESTING_GUIDE.md`／`AI_OPTIMIZATION_PROMPTS.md` 不再灌进知识库（`progress_sync.KNOWLEDGE_EXCLUDE`）。它们写满了「左志诚被谁挖走了命丛」这类用来说明踩坑的剧情引用，反复出现在剧情问题的命中里——**我们写的踩坑文档变成了检索噪声**。这类文档是给人读的，不是给检索用的。

## 小说实体索引（五层检索）

**解决的问题**：问「小说里出现过哪些命丛」时，向量检索几乎无用。实测数据（《寂静杀戮》1936 块）：

| 检索方式 | 命中 | 精度 |
|---|---|---|
| 向量搜「命丛有哪些」 | top3 全是无关 PDF，小说排第四 | ≈0（sim 0.023 vs 0.025 无区分力）|
| FTS5 搜**类名**「命丛」 | 308 / 1936 块 | **15.9%**，等于没筛 |
| FTS5 搜**专名**「银河灵潮」 | 1 块 | **~100%** |

两个根因：① 枚举式问法（"有哪些"）与叙事文本（"当看到那蜷缩起来的怪物时"）语义分布不重叠；② 具体实体有自己的专名，与类名不构成固定组合——原文是「你的命丛在左眼里，这个命丛，被称之为'夜海'」，专名与类名相隔 8 字，也有「命丛夜海」直连、「就剩下夜海这个命丛了」倒序。含「夜海」的 69 块里有 26 块**根本不含"命丛"二字**，搜类名必漏。

**方案**：先建专名索引（`novel_entities` 表），把低精度的类名匹配转成高精度的专名匹配。

```
阶段一 · 建索引（一次性）
  命名句定位（纯规则）  308 块 → 44 块候选，缩 86%
    ├ 命名句模式：被称之为X / 叫做X / 名为X / 引号包裹
    ├ 距离约束：专名须在类名 40 字内（否则全书专名都会被抽进来）
    └ 枚举句补漏：「这四种命图分别是A，B，C以及D」——命名句模式抓不到
  LLM 抽专名          只做一件事：从候选块里挑出真专名，输出仅含名字
  跨类去重            同名归属证据更强的一类（枚举句 > 共现频次）
  用户确认            verified=1 的不被后续自动抽取覆盖，note 优先于原文

阶段二 · 检索（每次提问，零 LLM）
  第0层 意图+实体识别  枚举意图（哪些/列举/清单/多少种）→ 查实体表拿专名
  第1层 专名精确召回   逐个专名 FTS5 检索，不设 top_k（宁多勿漏）
  第2层 块内定位裁剪   只取含专名的句子±1句；与"分为/共有/要求"共现的加权
  第3层 聚合去重       同一设定被逐字重复引用时只留信息量最大的
  第4层 预算裁剪+报告  按预算填充，并附完整度："收录 N 个，原文提到该有 M 个"
```

**效果对比**（同一个问题）：

| | 改造前 | 改造后 |
|---|---|---|
| 命丛 | 1 个（冥王蛇） | **27 个**，每个带作用描述 |
| 命图 | 1 个（银河灵潮） | **8 个**，含各自所需命丛数 |
| 缺口 | 「没有可靠依据，不敢硬凑」 | 「原文提到七大神命丛，目前只确认 N 个，这个缺口是资料本身不全」 |

《寂静杀戮》已抽出 **160 个实体**：命丛 27 · 命图 8 · 功法 72 · 势力 53。
问「有哪些势力」时她能按地方帮派 / 官方 / 中原门派 / 异族分类答出，并报告
「原文提到'三大势力'但清单里没明确对应」这类缺口。

**四类实体的触发词差异**（实测调出来的）：

| 类型 | 触发词 | 备注 |
|---|---|---|
| 命丛 / 命图 | `命丛` / `命图` | 专有类名，单词即可 |
| 功法 | `道术` `功法` `秘籍` `武功` `招式` | 同一功法在不同语境下类名不同 |
| 势力 | `门派` `兵团` `宗门` `教派` `帮派` `势力` `组织` | **不能用单字**「宗」「教」——会命中"宗旨""教训"，实测候选里混进了"孙悟空""恶意""封印" |

第 4 层的完整度报告是关键：**缺口可见才谈得上诚实**。她现在能说清"确认了几个、原文该有几个、差的是什么"，而不是笼统地推回给用户。

**为什么存索引而不存答案**：实体表存的是**名字**（客观、唯一、不随提问变化），问"有哪些"和问"夜海怎么修炼"用同一张表。若缓存问答结果，会随提问维度爆炸且不同批次互相矛盾。检索每次重做——纯 SQL 零 LLM，重做不心疼。

> 关键原则（LESSONS 6.6/6.7）：**确定事实走注入，不靠检索**——状态、身份、偏好类必须每次必达。
>
> 前八条通道都是"关于用户"的，第十条是唯一"关于她自己"的——没有它，每轮对话里的小月都是刚出生的。

## 模块说明

| 目录 | 内容 | 技术栈 |
|------|------|--------|
| `server/` | 聊天编排、记忆闭环、RAG 知识库、行为统计、反思/备份定时任务、API 鉴权（35 个服务模块） | FastAPI · SQLite · sqlite-vec · APScheduler |
| `server/benchmarks/` | 检索评测（Hit@k/MRR，8 题测试集）+ 对话回归集（13 题，含 token 用量与成本） | — |
| `server/scripts/` | 文档同步进知识库（docs/*.md → 可检索） | — |
| `collector/` | 三通道采集（浏览器覆盖 Chromium 系全 profile + Firefox）、隐私脱敏、断网落盘、心跳上报与通道停滞告警、Win32 API 封装 | Python · ctypes |
| `common/` | 跨端共享：脱敏规则（`redact.py`）、执行器文件操作与安全判据（`file_ops.py`）、快捷启动器（`launcher.py`） | Python |
| `desktop/` | 自绘机器人（三套皮肤/呼吸/眨眼/状态灯）、气泡面板（Markdown/可缩放/最大化/尺寸记忆）、快捷启动器、本地执行器、托盘、健康检查、开机自启 | PySide6 · Qt6 |
| `docs/` | 11 份文档（方案/参考/评审/提问/部署/踩坑/进度/运维/研究/测试 + 本文） | Markdown |
| `scripts/` | 服务器部署、开机自启、桌面打包 | bash · PowerShell |

### 服务端模块速查（`server/app/services/`）

按职责分组，共 35 个模块：

| 分组 | 模块 | 职责 |
|---|---|---|
| **记忆与反思** | `consolidation` `fact_extract` `analyzer` | 4h 摘要整合、事实三元组提取、按 kind 分留存期淘汰 |
| | `self_reflect` `profile` `weekly_reflect` `daily_summary` | 纠正→教训（去重+分类）、四维画像、周报、每日小结 |
| **检索增强** | `novel_entities` | 小说专名索引 + 五层检索（本文重点） |
| | `subjective_time` `cooccurrence` | 事件锚点替代日期、一跳共现扩散 |
| **拟人化** | `mood` `self_state` `initiative` `greeting` | 情绪轨迹与隔日跟进、她自己的状态、主动开口、个性化问候 |
| | `identity_guard` `few_shot` `jargon` | 人格安全、风格范例、术语口径 |
| **任务与追踪** | `goals` `unresolved` `concern_tracker` `worklog` `reminders` | 目标、未解决问题、关切话题、工作日志、定时提醒 |
| **执行器** | `executor` `confirm` | 指令队列（原子认领+超时释放）、破坏性操作确认层 |
| **产出** | `documents` `resume` `novel_writing` `chapter_analysis` | 文档生成、简历优化（.docx）、小说写作辅助、章节分析+跨章剧情存档 |
| **垂直领域** | `fitness` | 健身减脂：台账 + 21 张权威知识卡（含训练学容量/次数/恢复） |
| **基础设施** | `sanitize` `backup` `behavior_context` `qq_push` `progress_sync` `message_search` | 脱敏、热备份、行为注入、QQ 推送、进度同步、全文搜索 |

### 数据表清单（SQLite，26 张业务表）

| 类别 | 表 | 说明 |
|---|---|---|
| 记忆核心 | `memories` `memories_fts` `memory_vectors` | 原文 + FTS5 倒排 + 向量（含 `hit_count` 使用反馈）|
| 长期知识 | `facts` `profile` `lessons` `concerns` `jargon_terms` `style_examples` | 六类注入通道，均按 `user_id` 隔离 |
| 知识库 | `knowledge_chunks` `knowledge_fts` `chunk_vectors` `documents` | RAG 三件套 + 生成的文档 |
| 小说 | `novel_entities` `novel_facts` `writing_log` `chapter_notes` | 专名索引 / 人工修订设定卡 / 写作台账 / 章节剧情存档（每章摘要+未回收伏笔） |
| 行为 | `behavior_events` `work_log` `mood_log` | 采集事件 / 手动日志 / 情绪轨迹 |
| 任务 | `goals` `unresolved_issues` `reminders` `executor_commands` | 目标 / 未解决 / 提醒 / 指令队列 |
| 反思归档 | `weekly_reports` `daily_summaries` | 周报 / 每日小结 |
| 垂直领域 | `fitness_log` `fitness_facts` | 健身台账 / 知识卡 |
| 主动性 | `initiative_log` | 主动开口台账（上限/降频依据）|

## 快速开始

### 前置（一次性）

1. Windows 与服务器都加入同一私有加密网络（服务走内网，不暴露公网）
2. 配置两端开机自动连接，并确认服务器与 Windows 可通过私网地址互通

### 1. 服务器端

```bash
git clone git@github.com:<用户名>/<仓库名>.git
cd personal-ai-assistant/server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env   # 填 LLM/Embedding Key + 生成 API_TOKEN
# 当前实例采用手工进程启动；systemd 新装模板见 scripts/deploy_server.sh
nohup .venv/bin/python run.py > /tmp/assistant.log 2>&1 &
```

`.env` 要点：`API_TOKEN`（32 字节随机）、`LLM_BASE_URL`（OpenCode Go 或 DeepSeek 官方）、`LLM_MODEL`（普通聊天）、`VISION_LLM_MODEL`（图片识别）、`EMBEDDING_DIMENSION`（智谱 embedding-3 = 2048）。实际运行状态与排查命令见 [OPS](docs/OPS.md)。

### 2. 采集器（Windows）

```powershell
git clone https://github.com/<用户名>/<仓库名>.git
cd personal-ai-assistant\collector
python -m pip install -r requirements.txt
# .env: SERVER_URL=http://<服务器私网IP>:8000 + API_TOKEN + GIT_REPOS
python main.py
# 开机自启（管理员 PowerShell）:  ..\scripts\install_autostart.ps1
```

### 3. 桌面机器人（Windows）

```powershell
cd ..\desktop
python -m pip install -r requirements.txt
python main.py
```

聊天面板操作：拖八方向边缘/角落缩放、按住标题栏移动、点 □ 或双击标题最大化（尺寸自动记忆）、📌 置顶、Esc/✕ 关闭。

快捷启动器（对话即管理）：

```
记住 打开B站 = https://www.bilibili.com     # 注册网页（裸域名自动补 https）
记住 打开示例应用 = D:/Program/YourApp/YourApp.exe # 注册应用（显式注册=用户授权，可出白名单）
记住 在B站搜索 = https://search.bilibili.com/all?keyword={q}  # 搜索模板
记住 用chrome打开GitHub = github.com        # 指定浏览器
打开B站 / 在b站搜索 关键词 / 用chrome打开GitHub  # 使用（本地直行，零延迟）
我的常用 / 忘掉B站                           # 列表（按使用排序）/ 删除
```

### 4. 同步项目文档进知识库（让机器人知道项目进展）

```bash
cd server && git pull
.venv/bin/python scripts/sync_docs_to_knowledge.py
```

## 测试与质量

| 项 | 状态 |
|----|------|
| 服务端隔离回归 | **1004 passed / 2 skipped**（`cd server && .venv/bin/python -m pytest tests/ -q`；视觉用例包含在此回归，未重复调用真实视觉服务） |
| QQ 图片专项 | **5 passed**（`pytest qq/astrbot_plugin_xy/test_main.py -q`；仅使用 AstrBot/HTTP 桩） |
| 桌面图片专项 | **6 passed**（`pytest desktop/tests/test_image_input.py -q`；对应选图、剪贴板、图片-only、multipart 与临时文件清理） |
| 输出格式 | QQ 不渲染 Markdown，回复出口做确定性去标记转换（`plain_text.strip_markdown`）——prompt 禁令是概率性的，实测线上仍大量出现 `**加粗**` 与 `- 列表`。转换而非删除，信息不丢；写文档/简历流程不受影响 |
| 测试隔离 | conftest 两层护栏：默认库挪出生产库 + `connect()` 上的 time-of-use 拦截器（测试期连生产库直接 RuntimeError）。曾因 14 个测试文件的隔离失效清空过真实库，见 LESSONS 6.31 |
| 检索评测 | `benchmarks/eval_retrieval.py`：基线 MRR 0.906 → 混合 0.938 |
| 对话回归集 | `benchmarks/chat_regression.py`：10 题固定问题集（身份/记忆召回/防幻觉/命令/纠正/格式），报告含真实 token 用量与成本；`--dry-run` 跑临时库零污染。一次约 0.09 元 |
| 踩坑沉淀 | 20+ 个真实问题，每个配"根因+修复+教训"（LESSONS.md） |
| 依赖 | requirements 全精确锁版 |

## 图片识别一期验收清单

以下清单按用户入口拆分；`[x]` 仅记录已经完成的本地/隔离测试证据，不把真实视觉调用或 QQ 发消息重复作为文档验收动作。验收基准日：**2026-09-05**。

### 桌面端

- [ ] 通过“图片”选择 JPEG/PNG/WebP，确认附件名、清除按钮和大小提示正常。
- [ ] 发送“图片 + 文字”和图片-only；确认图片请求直接走 multipart，不进入本地执行器。
- [ ] 用“粘贴”或 `Ctrl+V` 粘贴剪贴板图片，发送成功、失败、取消后确认临时 PNG 已清理，原始图片不被修改。
- [x] `pytest desktop/tests/test_image_input.py -q`：**6 passed**。

### QQ

- [ ] 主人私聊发送图片并带问题，确认回复来自 `/api/chat/vision`；访客私聊可识图但不能读取主人记忆或调用主人专属功能。
- [ ] 群聊图片在任何上传前静默并 `stop_event`，不触发 API/默认 LLM。
- [ ] 优先验证 NapCat `get_file`；失败时再验证 CDN 直连/`download_proxy` 代理兜底，并检查临时文件最终删除。
- [ ] 验证 JPEG/PNG/WebP 的文件头、MIME、10MB 上限、错误提示，以及 `QQ_API_TOKEN` + `identity_secret` 对齐的 HMAC/request_id。
- [x] `pytest qq/astrbot_plugin_xy/test_main.py -q`：**5 passed**。

### Web/API

- [ ] 先执行无副作用的 `GET /api/health` 与 `GET /api/ready`；不得把真实视觉调用放进健康检查。
- [ ] 用 `multipart/form-data` 验证 `image`、可选 `message`、必填 `request_id` 和可选 `user_id`；确认格式、MIME、10MB 边界按预期返回。
- [ ] 配置角色 token 后验证 `owner`/`internal`/`qq` 可访问，`collector`/`executor` 被拒绝；QQ 请求还需通过 HMAC 身份校验。
- [ ] 用同一用户同一 `request_id` 重试确认成功响应复用；更换消息或图片返回 `409`，视觉失败不缓存并可安全重试。
- [x] `cd server && .venv/bin/python -m pytest tests/ -q`：**1004 passed / 2 skipped**，视觉用例包含在此回归。

### MCP

- [ ] 明确一期边界：MCP 不承载图片上传；图片入口只验收桌面端、QQ、Web/API，MCP 只验收既有本地工具链。
- [ ] 保持 `MCP_ENABLED=false` 时关闭；启用后只允许独立 stdio 进程，`MCP_STDIO_ROLE` 只能是 `owner`/`internal`，`MCP_STDIO_USER_ID` 不得绕过主人边界。
- [ ] 检查 MCP stdout 只输出协议内容、普通日志走 stderr；确认读取权限、显式确认闸门和未批准的 shell/Python/删除工具仍关闭。
- [ ] 检查 `mcp_audit_logs` 记录 user/role/tool/request_id/client_name、成功失败和耗时，参数只保存脱敏摘要，不保存图片或敏感全文。

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
| 已完成 | 第 11 课 Agent 工具链（执行器队列 + 桌面本地执行）/ 第 12 课 Goal 系统 + unresolved 追踪 / 第 13 课 文件手 + 脚本脚 | ✅ |
| 已完成 | 执行器安全加固（白名单归一化 / 入队强制校验 / open 黑名单 / 原子认领） | ✅ |
| 已完成 | 第 14 课 快捷启动器（别名注册常用软件/网页/搜索模板 + 指定浏览器 + 失败建议） | ✅ |
| 已完成 | 第 8 课 QQ 私聊接入（AstrBot 插件 + 提醒 QQ 推送唯一通道 + 群聊零暴露白名单） | ✅ |
| 已完成 | 全量代码审计整修（执行器绕过 / 命令误吞 + 确认层 / 脱敏合并 / git 游标 / 淘汰与备份 / 桌面稳定性 / 采集可靠性，302→421 测试） | ✅ |
| 已完成 | 测试隔离修复（14 个文件的库隔离失效曾清空生产库）+ conftest 两层护栏 | ✅ |
| 已完成 | 拟人化：教训去重与身份优先 / 隔日情绪跟进 / 自我状态注入 / 主动开口通道（默认关闭，421→459 测试） | ✅ |
| 已完成 | 主观时间锚定 / 身份守卫 / 使用反馈 hit_count / 一跳共现扩散（467→548 测试） | ✅ |
| ⭐⭐ | 第 6 课 CI / 第 7 课仪表盘 | ⏳ |
| ⭐ | 注入可观测性：单通道超时护栏 + 慢注入耗时分解 + 最小 trace 表 | ⏳ |
| ⭐ | 拟人化续：打字节奏、表情联动（桌面端，需实机视觉验证） | ⏳ |

## 安全

- 全 API `API_TOKEN` 鉴权（32 字节随机，不入库）
- 服务仅私有专线可达；公网仅暴露 SSH 22（密钥 + fail2ban）
- 事件出网前本地脱敏（密码/token/手机号/邮箱/各类云凭证；规则单一来源 `common/redact.py`，两端共用防漂移）
- 执行器纵深防御：扩展名黑名单（归一化尾点/尾空格/NTFS 数据流，防变形绕过）→ 白名单根目录校验（realpath 解析链接）→ 破坏性操作二次确认；别名解析对路径样式目标不生效，避免劫持
- 敏感信息（IP/密钥/token）只存本地笔记，**禁止入仓库**（每次提交前终扫）

## 许可证

私有项目，保留所有权利（见 [LICENSE](LICENSE)）。代码仅供本人使用与学习，
可阅读源码做技术交流，但未经书面许可不得复制、分发或用于商业用途。

依赖的第三方库遵循其各自的开源许可证。设计上参考过的外部项目见
[LESSONS 6.34](docs/LESSONS.md)——仅借鉴公开文档描述的概念，未复制代码。
