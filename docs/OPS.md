# 运维手册 —— 组件启停与排查

> 三个组件的日常运维命令速查。所有路径以实际部署为准（示例为默认位置）。

## 组件总览

| 组件 | 位置 | 管理方式 | 日志 |
|------|------|----------|------|
| 服务端 | JD 服务器 `/root/personal-ai-assistant` | systemd 未配，nohup 后台 | `/tmp/assistant.log` |
| 采集器 | Windows `F:\Projects\git\personal-ai-assistant\collector` | 任务计划 PAA-Collector | `collector\logs\collector.log` |
| 机器人 | Windows `F:\Projects\git\personal-ai-assistant\desktop` | 任务计划 PAA-Robot | `desktop\logs\desktop.log` |

---

## 一、服务端（JD 服务器，SSH 登录后执行）

### 重启

```bash
cd /root/personal-ai-assistant/server
kill $(pgrep -f 'run\.py$')        # 停（$ 锚定防误杀）
setsid nohup .venv/bin/python run.py </dev/null > /tmp/assistant.log 2>&1 &
```

### 健康检查

```bash
curl -s http://127.0.0.1:8000/api/health
```

### 看日志

```bash
tail -20 /tmp/assistant.log                       # 最近日志
grep "定时任务已注册" /tmp/assistant.log           # 查定时任务
grep -E "ERROR|Traceback" /tmp/assistant.log | tail -5   # 查错误
```

### 更新代码并重启（git pull + 重启一条龙）

```bash
cd /root/personal-ai-assistant && git pull
kill $(pgrep -f 'run\.py$'); sleep 2
cd server && setsid nohup .venv/bin/python run.py </dev/null > /tmp/assistant.log 2>&1 &
sleep 4 && tail -5 /tmp/assistant.log
```

---

## 二、采集器（Windows，管理员 PowerShell）

### 重启（推荐方式：任务计划隔离，不误伤机器人）

```powershell
Stop-ScheduledTask -TaskName "PAA-Collector"
Start-ScheduledTask -TaskName "PAA-Collector"
```

### 查日志

```powershell
Get-Content F:\Projects\git\personal-ai-assistant\collector\logs\collector.log -Tail 10
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

---

## 三、机器人（Windows，管理员 PowerShell）

### 重启

```powershell
Stop-ScheduledTask -TaskName "PAA-Robot"
Start-ScheduledTask -TaskName "PAA-Robot"
```

### 查日志（黑匣子）

```powershell
Get-Content F:\Projects\git\personal-ai-assistant\desktop\logs\desktop.log -Tail 10
```

### 手动退出

右键机器人 → 退出（或托盘图标右键 → 退出）。正常退出不触发任务重启。

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
| 聊天无响应 | ① 服务器 health ② 机器人状态灯（红=断线）③ 服务器日志 |
| 采集器不推数据 | ① collector.log ② 任务 LastTaskResult ③ 服务器 health 心跳 |
| 机器人没出现 | ① 任务 State ② desktop.log 黑匣子 ③ 任务 Actions 路径 |
| 周报没生成 | ① 服务器日志查 weekly_reflect ② 手动 `curl -X POST /api/reports/generate` |
| 全部正常但数据重复 | 服务器幂等兜底，无需处理 |
