# QQ 接入运维手册 —— NapCat / 自建 OneBot 网关

> 本文记录当前新链路。AstrBot 宿主、`astrbot_plugin_xy` 插件与 MaiBot 已于 2026-09-15 删除，旧章节不再作为运行步骤。
> 当前仓库只提供网关代码和 systemd 模板；实际 NapCat reverse HTTP 配置、`.env` 密钥和服务启停需在部署机本地完成。

## 一、当前数据流

```text
手机 QQ
  ↕
NapCat（QQ 登录 + OneBot）
  ├─ reverse HTTP 事件 → personal-qq-gateway（默认 127.0.0.1:3101）
  │                         ├─ 群白名单 / 前缀 / 真实 At / Reply / follow-up 门禁
  │                         ├─ Bearer + QQ HMAC → FastAPI /api/chat
  │                         └─ 非空回复 → NapCat send_group_msg/send_private_msg
  └─ OneBot HTTP action ← 网关与服务端的发送/Reply 查询

FastAPI personal-assistant.service（默认 127.0.0.1:8000）
  └─ 身份、群作用域、request_id 幂等、聊天编排、最终安全审校
```

网关和服务端各有一个明确职责：网关是 OneBot 事件的唯一 `group_directed` 判定者；服务端不重新解析 QQ 消息段，但仍拒绝主人身份进入群作用域，并保留最终权限、安全和上游失败处理。

## 二、确定性群门禁

群消息必须先通过 `QQ_GATEWAY_GROUP_ALLOWED_IDS`；空白值不接收任何群，只有单独配置 `*` 才全群接收。

启用 `QQ_GATEWAY_GROUP_REQUIRE_MENTION=true` 时，下列任一条件成立才设置 `group_directed=true`：

1. 文本以 `QQ_GATEWAY_GROUP_TRIGGER_PREFIX` 开头。
2. OneBot `at` 消息段的 `data.qq` 等于本账号 `self_id`。
3. OneBot `reply` 消息段引用的消息，其 `get_msg` 返回发送者等于本账号 `self_id`。
4. 同一群同一用户在服务端刚成功回复后，命中有限的 `interaction.followup` 窗口。

无法取得本账号 ID、Reply 查询失败、At 指向他人、纯文本出现机器人名字，全部 fail-closed。直接 @ 不会被 planner 的 `ignore/interject` 静默，但仍受身份校验、空回复、上游失败和每群小时滥用上限约束。

非直达消息默认不调用服务端；只有显式打开 `QQ_GATEWAY_GROUP_INTERJECT_ENABLED` 才以 `group_directed=false` 透传，由服务端既有主动插话闸门决定。

## 三、脱敏配置

所有真实 token、HMAC secret、QQ 号和地址只写部署机 `.env`，不写仓库。

| 配置 | 作用 |
|---|---|
| `QQ_PUSH_URL` / `QQ_PUSH_TOKEN` | NapCat OneBot HTTP action 地址和 token；提醒推送与网关发送共用 action 出口 |
| `QQ_API_TOKEN` | 访客/群请求的 Bearer token |
| `OWNER_API_TOKEN` 或 `API_TOKEN` | 主人私聊 Bearer token |
| `QQ_IDENTITY_SECRET` | 访客/群请求 QQ 号、时间戳、request_id 的 HMAC 密钥 |
| `QQ_ADMIN_ID` | 主人 QQ 号；群请求即使发送者相同也仍使用访客角色 |
| `QQ_GATEWAY_HOST` / `QQ_GATEWAY_PORT` | 网关监听地址，默认 `127.0.0.1:3101` |
| `QQ_GATEWAY_INBOUND_TOKEN` | NapCat reverse HTTP 事件推送 token；非回环监听必填 |
| `QQ_GATEWAY_SELF_ID` | 可选的本账号 ID；不匹配事件 self_id 时拒绝唤醒 |
| `QQ_GATEWAY_ONEBOT_URL` / `QQ_GATEWAY_ONEBOT_TOKEN` | 网关调用 NapCat action（`get_msg`/`send_*`）的出口与鉴权；留空回退 `QQ_PUSH_URL` / `QQ_PUSH_TOKEN` |
| `QQ_GATEWAY_OWNER_ID` | 网关识别主人私聊的 QQ 号；留空回退 `QQ_ADMIN_ID`；群聊始终使用访客身份 |
| `QQ_GATEWAY_GROUP_ALLOWED_IDS` | 群白名单；空值拒绝所有群，`*` 才全开 |
| `QQ_GATEWAY_GROUP_REQUIRE_MENTION` | 是否要求真实 At/Reply/前缀，默认 `true` |
| `QQ_GATEWAY_GROUP_TRIGGER_PREFIX` | 可选群触发前缀 |
| `QQ_GATEWAY_GROUP_INTERJECT_ENABLED` | 是否允许非直达消息进入服务端主动插话，默认 `false` |
| `QQ_GATEWAY_GROUP_MAX_REPLIES_PER_HOUR` | 每群成功发送上限，默认 30 |
| `QQ_GATEWAY_FOLLOWUP_WINDOW_SECONDS` / `QQ_GATEWAY_FOLLOWUP_MAX_MESSAGES` | 短时续话窗口，默认 90 秒/1 条 |
| `QQ_GATEWAY_API_BASE` | FastAPI 地址，默认 `http://127.0.0.1:8000` |

`.env.example` 已包含全部无密钥配置键。网关启动入口为：

```bash
/opt/personal-ai-assistant/server/.venv/bin/python -m qq.onebot_gateway
```

## 四、systemd 与 NapCat 配置

仓库模板为 [`scripts/personal-qq-gateway.service`](../scripts/personal-qq-gateway.service)。部署机同步 GitHub 后，确认 `.env` 已配置，再执行：

```bash
sudo install -m 0644 scripts/personal-qq-gateway.service /etc/systemd/system/personal-qq-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now personal-qq-gateway
systemctl status personal-qq-gateway --no-pager
curl -s http://127.0.0.1:3101/health
```

NapCat reverse HTTP server 的事件目标应指向网关监听地址，并配置与 `QQ_GATEWAY_INBOUND_TOKEN` 相同的 access token；OneBot action 地址仍由 `QQ_PUSH_URL` 指向 NapCat HTTP server。NapCat 使用 host 网络时，容器内外的本机端口可直接互通；不要把网关端口或 FastAPI 端口公开到公网。

服务日志：

```bash
journalctl -u personal-qq-gateway -n 50 --no-pager
journalctl -u personal-assistant -n 50 --no-pager
```

本次实现不自动改 NapCat 配置、不自动启用 systemd、不重启现网服务。启用前先备份 NapCat 本地配置，并确认登录态不会因容器重启丢失。

## 五、服务端 HTTP 契约

网关调用现有 `POST /api/chat`，透传：

- `message`、`user_id`、`request_id`
- 群消息额外透传 `group_id`、`group_directed`
- 访客/群请求使用 `Authorization: Bearer QQ_API_TOKEN` 和 `X-QQ-User-ID`、`X-QQ-Timestamp`、`X-QQ-Request-ID`、`X-QQ-Signature`
- 主人私聊只使用 `OWNER_API_TOKEN`/兼容 `API_TOKEN`，不使用访客 HMAC

服务端仍执行时间窗口、签名一致性、主人冒充拒绝、群作用域拒绝、同一用户同一 `request_id` 幂等和群数据隔离。网关在发送成功后记录有界 request cache，防止 NapCat 重试同一事件造成重复发送。

## 六、当前限制与排障

- 本轮网关只处理 OneBot 文本消息；图片、语音、视频和文件事件安全忽略，不会把媒体占位符误当作普通文本。媒体入口需另立有限任务。
- 群无回复：依次检查 `QQ_GATEWAY_GROUP_ALLOWED_IDS`、事件 self_id、At/Reply 的 OneBot 消息段、Reply 的 `get_msg` action、服务端 `/api/health` 和两套 token/HMAC 配置。
- 返回 401/403：检查访客 Bearer、HMAC 的 user/timestamp/request_id 是否一致；群请求不能使用主人 token。
- 重复消息：网关按 OneBot message_id 生成稳定 request_id；服务端幂等和网关成功投递缓存都命中时不会重复发送。
- 服务端有回复但 QQ 无消息：检查 `QQ_PUSH_URL`、`QQ_PUSH_TOKEN`、NapCat `send_group_msg/send_private_msg` action 和容器日志。
- 任何排障日志都不得记录消息正文、QQ 号、token、HMAC secret 或完整请求体。

旧 AstrBot/MaiBot 配置、插件测试和图片处理步骤只可从 Git 历史回看，不能照本文执行。
