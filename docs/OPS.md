# 运维手册 —— 组件启停与排查

> 本手册以 **2026-09-15** 已核对的当前实例为准。服务端由 systemd 单元 `personal-assistant.service` 管理（2026-09-15 起，取代原手工进程方式）。
> QQ/NapCat 与自建 OneBot 网关的运维见 [`docs/QQ_OPS.md`](QQ_OPS.md)；旧 AstrBot/MaiBot 链路不再执行。

## 组件总览

| 组件 | 位置 | 管理方式 | 日志 |
|---|---|---|---|
| 服务端 | `/opt/personal-ai-assistant/server` | **systemd** `personal-assistant.service`（User=paa，端口 8000） | `journalctl -u personal-assistant` |
| 采集器 | Windows `F:\Projects\git\personal-ai-assistant\collector` | 任务计划 `PAA-Collector` | `collector\logs\collector.log` |
| 机器人 | Windows `F:\Projects\git\personal-ai-assistant\desktop` | 守护进程 `PAA-Robot-Supervisor` | `desktop\logs\desktop.log` + `faulthandler.log` |
| QQ 接入 | NapCat + `personal-qq-gateway`（薄 OneBot 网关） | NapCat 容器 + systemd `personal-qq-gateway.service` | `journalctl -u personal-qq-gateway` + NapCat 容器日志 |

> `scripts/deploy_server.sh` 内的 systemd unit 是新装模板；当前实例由部署目录 `/opt/personal-ai-assistant` 的 `personal-assistant.service` 托管（2026-09-15 现场核对过启动与 health/ready）。

---

## 一、服务端（JD 服务器，SSH 登录后执行）——systemd

### 启动

```bash
systemctl start personal-assistant
```

### 停止 / 重启

```bash
systemctl restart personal-assistant   # 先停后起
systemctl stop personal-assistant      # 仅停止
```

### 状态与日志

```bash
systemctl status personal-assistant --no-pager
journalctl -u personal-assistant -n 50 --no-pager
journalctl -u personal-assistant -f
```

服务端单元：`/etc/systemd/system/personal-assistant.service`（`User=paa`、`WorkingDirectory=/opt/personal-ai-assistant/server`、`EnvironmentFile` 提供 `PORT=8000`、已 enabled）。不要在服务端使用 `kill`/`pkill` 或手工 `nohup` 方式，避免与 systemd 的重启策略冲突。

### 健康与就绪检查

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ready
```

`/api/health` 是轻量存活探针；`/api/ready` 会检查数据库、调度器、LLM 配置和向量能力。视觉接口不应被放进健康检查，也不要为了“探活”重复调用外部视觉服务。

### 视觉配置无副作用核对

```bash
cd /opt/personal-ai-assistant/server
.venv/bin/python - <<'PY'
from app.config import settings
from app.main import app

print({
    "llm_model": settings.llm_model,
    "vision_llm_model": settings.vision_llm_model,
    "vision_max_image_bytes": settings.vision_max_image_bytes,
    "vision_timeout": settings.vision_timeout,
    "llm_key_count": len(settings.llm_api_key_values),
})
assert any(getattr(route, "path", "") == "/api/chat/vision" for route in app.routes)
print("/api/chat/vision route registered")
PY
```

只核对模型名、上限、超时和 Key 数量，不打印 Key/token/secret 原文。

### 服务端常见问题速查

| 现象 | 排查顺序 |
|---|---|
| 聊天无响应 | ① `/api/health` ② `/api/ready` ③ `systemctl status personal-assistant` ④ `journalctl -u personal-assistant -n 50 --no-pager` |
| 服务起不来 | 查看 `journalctl -u personal-assistant`；常见原因是 `.env` 配置校验失败、LLM Key 池为空、视觉上限/超时为非正数 |
| 图片接口路由不存在 | 执行上面的无副作用 route 检查；确认代码版本和启动目录是 `/opt/personal-ai-assistant/server` |
| 图片返回 400/413/415 | 400=缺 `image`/`request_id`、空文件或损坏文件；413=超过 `VISION_MAX_IMAGE_BYTES`；415=格式或 MIME 不支持 |
| 图片返回 401/403 | 核对 Bearer 角色 token；QQ 入口继续核对 `QQ_API_TOKEN`、`QQ_IDENTITY_SECRET`、时间戳和签名 request_id |
| 图片一直识别失败 | 先核对 `VISION_LLM_MODEL`、`VISION_TIMEOUT`、Key 数量和 `journalctl -u personal-assistant`；不要在排障循环里反复调用真实视觉服务 |
| 周报/小结没生成 | ① `journalctl -u personal-assistant` 查“定时任务” ② QQ 是否收到失败告警 ③ 仅在明确需要时手动 POST 生成接口 |

---

## 二、QQ OneBot 网关（JD 服务器）

仓库内网关入口为 `python -m qq.onebot_gateway`，默认监听 `127.0.0.1:3101`。它只处理文本事件，负责群白名单、真实 At/Reply/前缀门禁、`group_directed`、有限 follow-up 和回复限流；FastAPI 继续负责身份、群作用域和最终安全约束。

2026-09-16 已在部署机应用：`personal-qq-gateway.service` 已启用，NapCat 已配置 reverse HTTP 推送（`httpClients[xy-gateway]`）。重新部署或迁移到新机器时，从 GitHub 拉取后先确认 `.env` 已配置且不含于仓库，再执行：

```bash
sudo install -m 0644 scripts/personal-qq-gateway.service /etc/systemd/system/personal-qq-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now personal-qq-gateway
systemctl status personal-qq-gateway --no-pager
curl -s http://127.0.0.1:3101/health
```

网关排障：

```bash
journalctl -u personal-qq-gateway -n 50 --no-pager
systemctl restart personal-qq-gateway  # 仅配置或代码已同步且确认需要时
```

不要把 `QQ_GATEWAY_INBOUND_TOKEN`、`QQ_PUSH_TOKEN`、`QQ_API_TOKEN`、`QQ_IDENTITY_SECRET` 或真实 QQ 号写入仓库；不要把网关端口或 FastAPI 端口公开到公网。空群白名单拒绝所有群，Reply 查询失败和无法确认真实 At 时均拒绝触发。

---

## 三、采集器（Windows，管理员 PowerShell）

### 重启（任务计划隔离，不误伤机器人）

```powershell
Stop-ScheduledTask -TaskName "PAA-Collector"
Start-ScheduledTask -TaskName "PAA-Collector"
```

### 查日志

```powershell
# 注意 -Encoding UTF8：日志文件是 UTF-8，不带参数会显示中文乱码
Get-Content F:\Projects\git\personal-ai-assistant\collector\logs\collector.log -Encoding UTF8 -Tail 10
```

### 查任务状态 / 运行结果

```powershell
Get-ScheduledTask -TaskName "PAA-Collector" | Select-Object State
Get-ScheduledTaskInfo -TaskName "PAA-Collector" | Select-Object LastRunTime, LastTaskResult
```

### 验证数据在推（服务器上查）

```bash
sqlite3 /opt/personal-ai-assistant/server/data/assistant.db \
  "SELECT COUNT(*) FROM behavior_events WHERE start_ts >= datetime('now','-10 minutes');"
```

### 断电/强杀丢数据说明

采集器每 30 秒把内存事件队列快照落盘（`collector/cache/pending_snapshot.jsonl`），推送成功的事件不会重复上送。强杀/断电最多丢最后一个快照窗口（≤30s）内的入队事件，下次启动自动重放快照——无需人工干预。

---

## 四、机器人（Windows）

### 重启 / 查日志

```powershell
# 机器人由守护进程 PAA-Robot-Supervisor 拉起：直接杀进程会立即被拉回
# 正确做法：右键机器人 → 退出（正常退出不触发守护拉起）
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\desktop.log -Encoding UTF8 -Tail 10
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\supervisor.log -Encoding UTF8 -Tail 5   # 守护进程
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\faulthandler.log -Encoding UTF8        # 原生崩溃取证（空=无崩溃）
```

---

## 五、Tailscale（一般不用动）

```powershell
tailscale status                 # 连接状态
tailscale up --unattended        # 重连（断线时）
```

---

## 六、常见问题速查

| 现象 | 排查顺序 |
|---|---|
| 聊天无响应 | ① 服务器 `/api/health` ② 机器人状态灯（红=断线）③ 服务器 `journalctl -u personal-assistant` |
| 采集器不推数据 | ① collector.log ② 任务 LastTaskResult ③ 服务器 health 心跳 |
| 机器人没出现 | ① supervisor.log（守护是否拉起）② desktop.log 黑匣子 ③ faulthandler.log（原生崩溃？） |
| 周报没生成 | ① QQ 是否收到“定时任务失败”告警 ② 服务器 `journalctl -u personal-assistant` 查 weekly_reflect ③ 明确需要时手动 POST `/api/reports/generate` |
| QQ 提醒不响 | 见 `QQ_OPS.md` 排查节 |
| 全部正常但数据重复 | 服务器幂等兜底，无需处理 |

## 七、图片一期回归证据

- 服务端隔离回归：**1004 passed / 2 skipped**，视觉用例包含在内。
- QQ 图片专项：**5 passed**。
- 桌面图片专项：**6 passed**。

以上数字只记录已完成证据；日常运维不因更新文档而重复执行真实视觉调用。
