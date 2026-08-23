# Collector —— Windows 行为采集器

Python 守护进程，开机自启（任务计划程序）。采集三通道行为事件并推送到服务器：

| 通道 | 文件 | 说明 |
|------|------|------|
| 前台窗口 | `window_monitor.py` | Win32 API 轮询，5-10s，记录应用使用时长 |
| 浏览器历史 | `browser_history.py` | 复制 Chrome/Edge History 后增量读取 |
| git 提交 | `git_scanner.py` | 定时扫描配置的项目目录 `git log` 增量 |

## 启动

```bash
pip install -r requirements.txt
python main.py            # 前台运行调试
```

## 安装为开机自启（管理员 PowerShell）

```powershell
# 见 scripts/install_collector.ps1
.\scripts\install_collector.ps1
```

## 隐私说明

- 浏览器事件只保留 域名 + 标题关键词（最长 80 字符），正文不上传
- 所有数据只发往 `.env` 里配置的 `SERVER_URL`（建议 Tailscale 内网地址）
- 断网时事件缓存在本地 `cache/`，恢复后重试推送

## 实现状态

- [x] 目录骨架
- [ ] M2 三通道采集 + 推送
