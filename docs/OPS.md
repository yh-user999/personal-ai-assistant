# 运维手册 —— 组件启停与排查

> 各组件的日常运维命令速查。所有路径以实际部署为准（示例为默认位置）。
> QQ/NapCat/AstrBot 的运维见 `docs/QQ_OPS.md`。

## 组件总览

| 组件 | 位置 | 管理方式 | 日志 |
|------|------|----------|------|
| 服务端 | JD 服务器 `/root/personal-ai-assistant` | **systemd**（assistant.service） | `journalctl -u assistant` |
| 采集器 | Windows `F:\Projects\git\personal-ai-assistant\collector` | 任务计划 PAA-Collector | `collector\logs\collector.log` |
| 机器人 | Windows `F:\Projects\git\personal-ai-assistant\desktop` | **守护进程 PAA-Robot-Supervisor**（崩溃秒级拉起） | `desktop\logs\desktop.log` + `faulthandler.log` |
| QQ 接入 | 服务器 `/opt/AstrBot`（宿主）+ NapCat 容器 | 见 QQ_OPS.md | AstrBot 控制台 / NapCat 容器日志 |

---

## 一、服务端（JD 服务器，SSH 登录后执行）—— systemd 管理

### 首次配置 systemd（一次性）

```bash
sudo tee /etc/systemd/system/assistant.service > /dev/null <<EOF
[Unit]
Description=Personal AI Assistant FastAPI Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/personal-ai-assistant/server
ExecStart=/root/personal-ai-assistant/server/.venv/bin/python run.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now assistant
```

> 取代旧的 nohup/setsid 方式：systemd 提供开机自启、崩溃自动重启、
> journal 日志轮转——nohup 方式这三样都没有，服务器重启后服务不会回来。

### 日常启停 / 重启（git pull 一条龙）

```bash
sudo systemctl restart assistant          # 重启（改代码/配置后）
sudo systemctl status assistant           # 状态
journalctl -u assistant -n 20 --no-pager  # 最近日志
journalctl -u assistant -f                # 实时跟踪

cd /root/personal-ai-assistant && git pull && sudo systemctl restart assistant
sleep 4 && curl -s http://127.0.0.1:8000/api/health
```

### 健康检查

```bash
curl -s http://127.0.0.1:8000/api/health
```

### 常见问题速查

| 现象 | 排查顺序 |
|------|----------|
| 聊天无响应 | ① health ② 机器人状态灯（红=断线）③ `journalctl -u assistant -n 50` |
| 服务起不来 | `journalctl -u assistant -n 30`（常见：.env 新增必填项校验失败，错误信息会指明字段） |
| 周报/小结没生成 | ① `journalctl -u assistant \| grep "定时任务"` ② QQ 是否收到失败告警 ③ 手动 `curl -X POST http://127.0.0.1:8000/api/reports/generate` |

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

采集器每 30 秒把内存事件队列快照落盘（`collector/cache/pending_snapshot.jsonl`），
推送成功的事件不会重复上送。强杀/断电最多丢最后一个快照窗口（≤30s）内的入队事件，
下次启动自动重放快照——无需人工干预。

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
|------|----------|
| 聊天无响应 | ① 服务器 health ② 机器人状态灯（红=断线）③ `journalctl -u assistant -n 50` |
| 采集器不推数据 | ① collector.log ② 任务 LastTaskResult ③ 服务器 health 心跳 |
| 机器人没出现 | ① supervisor.log（守护是否拉起）② desktop.log 黑匣子 ③ faulthandler.log（原生崩溃？） |
| 周报没生成 | ① QQ 是否收到"定时任务失败"告警 ② journalctl 查 weekly_reflect ③ 手动 POST /api/reports/generate |
| QQ 提醒不响 | 见 QQ_OPS.md 排查节 |
| 全部正常但数据重复 | 服务器幂等兜底，无需处理 |
