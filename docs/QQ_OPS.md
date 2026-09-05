# QQ 接入运维手册 —— NapCat / AstrBot / 插件

> 本文以 **2026-09-05** 已核对的图片识别一期实现为准。文本私聊走 `/api/chat` JSON，图片私聊走 `/api/chat/vision` multipart；本文不含真实 token、HMAC secret、QQ 号或公网地址。

## 一、架构与数据流

```text
主人/访客手机 QQ
   ↕ 私聊
NapCat 容器（QQ 协议端，onebot HTTP :3100）
   ↕ onebot 事件
AstrBot 宿主（插件 astrbot_plugin_xy）
   ├─ 文本消息 → HTTP Bearer → /api/chat（JSON）
   └─ 图片消息 → 取回/校验图片 → /api/chat/vision（multipart）
                                      ├─ image
                                      ├─ message（可选）
                                      ├─ request_id
                                      └─ user_id（QQ 号）
小月 FastAPI 服务器（普通聊天模型 / 视觉模型分开配置）
```

反向通道：小月的定时提醒经 `qq_push` 服务 → NapCat onebot HTTP → 主人 QQ。
提醒推送使用 `QQ_PUSH_TOKEN`，与入站聊天使用的 `QQ_API_TOKEN` 是两条不同链路。

### 图片请求处理顺序

1. 插件识别 AstrBot `Image` 组件，清理图片占位符和 URL，只保留用户文字作为 caption。
2. 取图优先使用组件本地路径/文件，再尝试图片 URL、组件 `get_file`，最后尝试 NapCat `get_file` 会话通道。
3. 下载按流限制大小，随后按文件头与 MIME 校验，只接受 JPEG、PNG、WebP，默认上限 10MB。
4. 以 `multipart/form-data` 上传到 `/api/chat/vision`，同时发送 `request_id`、`user_id` 和 QQ 身份 HMAC 头。
5. 请求结束后删除插件创建的临时文件；用户原始本地文件只读、不删除。

## 二、隐私铁律（插件白名单，多人支持）

- 群聊消息（包括图片）一律静默，插件先 `stop_event()`，零 API 调用。
- 私聊：主人和访客都可聊天；服务端按 QQ 号隔离记忆。访客可识图，但不能读取主人信息或调用主人专属功能。
- 主人专属功能（文件入库、执行器、提醒、工作日志等）只有 `owner_qq` 可用；门禁在服务端，不依赖插件自称身份。
- `owner_qq` 未配置 = 全拒（fail-closed）。
- 文件入库和图片识别是两个分支：陌生人发文件不会入库，但陌生人私聊图片可以进入视觉问答。

### AstrBot 会话白名单必须关闭

`cmd_config.json` → `platform_settings.enable_id_white_list` **必须为 `false`**。

白名单开启时，AstrBot 的 `whitelist_check` 会在插件前拦截陌生人私聊，多人支持和访客图片识别都会失效。群聊静默由 xy 插件自身兜底：收到群聊事件后立即 `stop_event()`，阻止后续插件响应。

若发现群聊中有机器人响应，优先检查插件版本至少为 v1.4.1，以及 xy handler 是否早于其他可能回复的插件加载。

## 三、日常操作

### NapCat 扫码登录 / 掉线恢复

1. 通过 AstrBot 宿主的 NapCat WebUI 或容器日志获取二维码。
2. 手机 QQ 扫码登录。
3. 容器重启后可能掉登录，重新扫码即可；不要为了图片识别临时公开 HTTP 端口。

### AstrBot 插件配置

| 项 | 说明 |
|---|---|
| `owner_qq` | 主人 QQ 号（纯数字字符串）；只用于主人专属功能和 fail-closed 判断 |
| `api_base` | 小月服务根地址，同机通常为 `http://127.0.0.1:8000` |
| `api_token` | 入站聊天 Bearer token；拆分鉴权时应与服务器 `QQ_API_TOKEN` 一致，不要填出站 `QQ_PUSH_TOKEN` |
| `identity_secret` | 与服务器 `QQ_IDENTITY_SECRET` 一致；用于签名 QQ 号、时间戳和 `request_id` |
| `onebot_http` | NapCat onebot HTTP 地址，图片/文件会话下载用，默认 `http://127.0.0.1:3100` |
| `onebot_token` | NapCat onebot HTTP token |
| `vision_timeout` | 图片取回、下载和 `/api/chat/vision` 的独立超时，默认 90 秒 |
| `vision_max_image_bytes` | 插件侧图片上限，默认 10485760（10MB）；下载和上传前均限制 |
| `download_proxy` | QQ CDN 直连 502 时的代理兜底；当前插件未填写时回落本机 clash `http://127.0.0.1:7890`，如环境不同请显式填写可用代理 |
| `container_path_map` | NapCat 容器路径 → 宿主路径映射；`容器前缀=宿主前缀`，多对用分号分隔 |

### 服务器 `.env`（脱敏示例）

```dotenv
# 入站：AstrBot → /api/chat 或 /api/chat/vision
QQ_API_TOKEN=<qq-api-token>
QQ_IDENTITY_SECRET=<shared-hmac-secret>
QQ_IDENTITY_MAX_AGE_SECONDS=300

# 出站：定时提醒 → NapCat
QQ_PUSH_URL=http://127.0.0.1:3100
QQ_PUSH_TOKEN=<napcat-onebot-token>
QQ_ADMIN_ID=<owner-qq-id>
```

QQ 插件构造的身份头包括 `X-QQ-User-ID`、`X-QQ-Timestamp`、`X-QQ-Request-ID`、`X-QQ-Signature`；签名载荷是 QQ 号、时间戳、`request_id` 逐行拼接后做 HMAC-SHA256。服务端还会检查时间窗口、表单 `request_id` 与签名 request_id 一致，以及 body `user_id` 与签名 QQ 号一致。

## 四、图片专项排障速查

| 现象 | 排查顺序 |
|---|---|
| 主人私聊图片无回复 | ① `/api/health` ② 插件 `api_base` ③ `api_token` ↔ `QQ_API_TOKEN` ④ `identity_secret` ↔ `QQ_IDENTITY_SECRET` ⑤ AstrBot `[xy]` 日志 |
| 访客私聊图片无回复 | ① `enable_id_white_list=false` ② 插件版本 ≥v1.4.1 ③ `identity_secret` 已配置 ④ 确认只发送图片问答，不是主人专属命令 |
| 群聊图片有响应 | 严重隐私问题：检查插件是否 ≥v1.4.1、xy 是否先于其他 handler；群聊必须在任何上传前 `stop_event()`，不应访问 `/api/chat/vision` |
| 图片提示“未能读取图片” | 检查 Image 组件是否只有 `file_id`；确认 `onebot_http`、`onebot_token` 和 NapCat `get_file` 可用，再看容器路径映射 |
| CDN 下载 502/超时 | 优先走 NapCat `get_file` 会话通道；仍失败时配置 `download_proxy`，确认本机 clash/代理可访问 QQ CDN |
| 格式或 MIME 不支持 | 只接受 JPEG/PNG/WebP；同时检查文件头和声明 MIME，SVG、改后缀文件、MIME 不匹配都会拒绝 |
| 图片超过大小上限 | 插件和服务端默认都是 10MB；先检查 `vision_max_image_bytes` 与 `VISION_MAX_IMAGE_BYTES`，下载阶段和上传阶段都可能返回超限 |
| 返回 401/403 | 401 通常是 Bearer token；403 通常是 QQ HMAC 缺失、过期、签名不匹配、QQ 号冒充主人或 request_id 不一致 |
| 重试返回 409 | 同一用户的同一 `request_id` 只能对应同一 caption 和同一图片；沿用原 request_id 重试同一请求，换内容要生成新 ID |
| 处理后 `/tmp` 有临时图片 | 检查插件 `[xy] 图片处理失败` 后的 finally 清理路径；组件提供的原始本地文件不由插件删除，插件自己创建的 `xy_vision_*` 应被清掉 |
| 文本正常、图片不通 | 两者路由不同：文本是 `/api/chat` JSON，图片是 `/api/chat/vision` multipart；不要只检查普通聊天接口 |

## 五、文本/图片回归证据

- QQ 图片专项：**5 passed**（`pytest qq/astrbot_plugin_xy/test_main.py -q`，使用 AstrBot/HTTP 桩，不发送真实 QQ 消息）。
- 服务端视觉用例包含在隔离回归：**1004 passed / 2 skipped**。

以上数字只记录已完成证据；日常排障不重复调用外部视觉服务。

## 六、升级注意

- 插件目录 `qq/astrbot_plugin_xy` 改动后需同步到 AstrBot 宿主并重载插件。
- 小月服务端视觉/QQ 配置变更后重启当前手工服务端进程，日志位置为 `/tmp/assistant.log`；不要假定存在 systemd 服务。
- NapCat 容器升级后重新扫码，检查 `onebot_http`、`onebot_token`、`container_path_map` 与新镜像路径约定。
- 任何 token、HMAC secret、QQ 号和公网地址只写本地配置，不写入仓库文档。
