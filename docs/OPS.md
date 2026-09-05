# 运维手册 —— 组件启停与排查

> 本手册以 **2026-09-05** 已核对的当前实例为准。服务端当前是手工进程，不把 systemd 示例当作正在运行的事实。
> QQ/NapCat/AstrBot 的运维见 `docs/QQ_OPS.md`。

## 组件总览

| 组件 | 位置 | 管理方式 | 日志 |
|---|---|---|---|
| 服务端 | `/root/personal-ai-assistant/server` | **手工进程**（uvicorn，端口 8000） | `/tmp/assistant.log` |
| 采集器 | Windows `F:\Projects\git\personal-ai-assistant\collector` | 任务计划 `PAA-Collector` | `collector\logs\collector.log` |
| 机器人 | Windows `F:\Projects\git\personal-ai-assistant\desktop` | 守护进程 `PAA-Robot-Supervisor` | `desktop\logs\desktop.log` + `faulthandler.log` |
| QQ 接入 | NapCat + AstrBot 宿主 | 见 `QQ_OPS.md` | AstrBot 控制台 / NapCat 容器日志 |

> `scripts/deploy_server.sh` 内的 systemd unit、服务用户和目录权限是新装模板；当前实例仍以 `/root/personal-ai-assistant/server` 下的手工进程和 `/tmp/assistant.log` 为准。

---

## 一、服务端（JD 服务器，SSH 登录后执行）——手工进程

### 启动

```bash
cd /root/personal-ai-assistant/server
nohup .venv/bin/python run.py >> /tmp/assistant.log 2>&1 &
echo $! > /tmp/assistant.pid
```

### 停止 / 重启

```bash
# 先查看当前实例 PID，避免误杀其他 Python 进程
pgrep -af '/root/personal-ai-assistant/server/.venv/bin/python run.py'

# 有记录的 PID 才执行停止
if test -f /tmp/assistant.pid; then kill "$(cat /tmp/assistant.pid)"; fi

# 重启（停止后重新执行启动命令）
cd /root/personal-ai-assistant/server
nohup .venv/bin/python run.py >> /tmp/assistant.log 2>&1 &
echo $! > /tmp/assistant.pid
```

### 状态与日志

```bash
pgrep -af '/root/personal-ai-assistant/server/.venv/bin/python run.py'
tail -n 50 /tmp/assistant.log
tail -f /tmp/assistant.log
```

不要按 systemd 服务名操作当前实例；若将来切换 systemd，先执行部署模板并同步更新本手册。

### 健康与就绪检查

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ready
```

`/api/health` 是轻量存活探针；`/api/ready` 会检查数据库、调度器、LLM 配置和向量能力。视觉接口不应被放进健康检查，也不要为了“探活”重复调用外部视觉服务。

### 视觉配置无副作用核对

```bash
cd /root/personal-ai-assistant/server
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
| 聊天无响应 | ① `/api/health` ② `/api/ready` ③ `pgrep` 确认进程 ④ `tail -n 50 /tmp/assistant.log` |
| 服务起不来 | 查看 `/tmp/assistant.log`；常见原因是 `.env` 配置校验失败、LLM Key 池为空、视觉上限/超时为非正数 |
| 图片接口路由不存在 | 执行上面的无副作用 route 检查；确认代码版本和启动目录是 `/root/personal-ai-assistant/server` |
| 图片返回 400/413/415 | 400=缺 `image`/`request_id`、空文件或损坏文件；413=超过 `VISION_MAX_IMAGE_BYTES`；415=格式或 MIME 不支持 |
| 图片返回 401/403 | 核对 Bearer 角色 token；QQ 入口继续核对 `QQ_API_TOKEN`、`QQ_IDENTITY_SECRET`、时间戳和签名 request_id |
| 图片一直识别失败 | 先核对 `VISION_LLM_MODEL`、`VISION_TIMEOUT`、Key 数量和 `/tmp/assistant.log`；不要在排障循环里反复调用真实视觉服务 |
| 周报/小结没生成 | ① `grep "定时任务" /tmp/assistant.log` ② QQ 是否收到失败告警 ③ 仅在明确需要时手动 POST 生成接口 |

---

## 二、采集器（Windows，管理员 PowerShell）

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
sqlite3 /root/personal-ai-assistant/server/data/assistant.db \
  "SELECT COUNT(*) FROM behavior_events WHERE start_ts >= datetime('now','-10 minutes');"
```

### 断电/强杀丢数据说明

采集器每 30 秒把内存事件队列快照落盘（`collector/cache/pending_snapshot.jsonl`），推送成功的事件不会重复上送。强杀/断电最多丢最后一个快照窗口（≤30s）内的入队事件，下次启动自动重放快照——无需人工干预。

---

## 三、机器人（Windows）

### 重启 / 查日志

```powershell
# 机器人由守护进程 PAA-Robot-Supervisor 拉起：直接杀进程会立即被拉回
# 正确做法：右键机器人 → 退出（正常退出不触发守护拉起）
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\desktop.log -Encoding UTF8 -Tail 10
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\supervisor.log -Encoding UTF8 -Tail 5   # 守护进程
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\faulthandler.log -Encoding UTF8        # 原生崩溃取证（空=无崩溃）
```

---

## 四、Tailscale（一般不用动）

```powershell
tailscale status                 # 连接状态
tailscale up --unattended        # 重连（断线时）
```

---

## 五、常见问题速查

| 现象 | 排查顺序 |
|---|---|
| 聊天无响应 | ① 服务器 `/api/health` ② 机器人状态灯（红=断线）③ `/tmp/assistant.log` |
| 采集器不推数据 | ① collector.log ② 任务 LastTaskResult ③ 服务器 health 心跳 |
| 机器人没出现 | ① supervisor.log（守护是否拉起）② desktop.log 黑匣子 ③ faulthandler.log（原生崩溃？） |
| 周报没生成 | ① QQ 是否收到“定时任务失败”告警 ② `/tmp/assistant.log` 查 weekly_reflect ③ 明确需要时手动 POST `/api/reports/generate` |
| QQ 提醒不响 | 见 `QQ_OPS.md` 排查节 |
| 全部正常但数据重复 | 服务器幂等兜底，无需处理 |

## 图片一期回归证据

- 服务端隔离回归：**1004 passed / 2 skipped**，视觉用例包含在内。
- QQ 图片专项：**5 passed**。
- 桌面图片专项：**6 passed**。

以上数字只记录已完成证据；日常运维不因更新文档而重复执行真实视觉调用。
