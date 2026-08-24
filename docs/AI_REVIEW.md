# AI 评审记录 —— 外部评估的采纳与拒绝

> 来源：外部 AI 对项目 v0.1 的评估文档（JD 服务器 `/root/wfy/ai个人助手.md`）。
> 本文档记录：哪些建议已采纳进代码、哪些进入路线图、哪些被拒绝及理由。
> 评审时间：2026-08

## 一、已采纳并实现（v0.2）

| # | 建议 | 落点 | 说明 |
|---|------|------|------|
| 1 | SQLite 并发写需 WAL 模式 | `models/database.py` | `PRAGMA journal_mode=WAL` + `busy_timeout=5000`：采集写入不再阻塞检索读取 |
| 2 | 重要性应加"主题活跃度补偿"（7 天内高频话题不衰减） | `core/memory.py` | 检索评分 = 相似度 × importance × 时间衰减 × topic boost（上限 1.5）。对应 Generative Agents 的 recency/importance/relevance |
| 3 | 隐私过滤：敏感信息本地脱敏 | `collector/privacy_filter.py` | 密码/token/API Key/银行卡/邮箱/手机号正则脱敏，事件出网前完成，可配置开关 |
| 4 | 采集停滞检测/告警 | `collector/pusher.py` + `api/events.py` | 轻量心跳（5 分钟）：各通道最近成功时间上报，`/api/health` 返回，无需 Prometheus |
| 5 | 热点检测 | `services/analyzer.py` | `top_topics()` 话题频次统计，已纳入周报 stats（"本周热点话题Top5"） |
| 6 | 记忆重复写入 | `core/memory.py` | 24h 内完全相同消息精确去重（相似度聚类去重进路线图） |

### 评审连带修复的真实 bug

- **pusher 线程安全 bug**：旧实现在工作线程调 `get_event_loop().call_soon_threadsafe()`，
  Python 3.10+ 在无事件循环的线程中会失败。重构为 `queue.Queue`（线程安全）+ 异步消费协程，
  顺带实现背压（队列满落盘）。这正是评审文档"背压机制"一条的价值所在。
- **`mount("/")` 吞路由 bug**：Starlette 按注册顺序匹配，静态页 `mount("/")` 注册在
  `/api/health` 之前导致 health 404。已调整顺序并加注释警示。
- **sqlite-vec 缺装时建库崩溃**：向量虚拟表建表失败现在自动跳过，基础功能不受影响。

## 二、进入路线图（v0.3+，未实现）

| 建议 | 理由 |
|------|------|
| 多粒度摘要（日汇总 → 周评 → 月复盘） | 有价值；v1 周报已覆盖核心需求，日/月粒度待数据积累后加 |
| 相似度聚类去重（向量重复检测） | facts 已有 UNIQUE 约束；memories 级聚类去重待向量库就绪后实现 |
| 知识图谱补强（因果/时序关系） | facts 三元组已是简化图谱；完整图谱是重活，需求明确后再上 |
| 监控指标体系（同步延迟/失败率） | 心跳已覆盖"死活"检测；精细指标个人场景收益低 |
| Docker 化服务端 | `deploy_server.sh`（systemd）已够用，多机部署时再做 |

## 三、拒绝的建议及理由

| 建议 | 拒绝理由 |
|------|----------|
| Collector 多进程重构（40h，"崩溃率>20%"） | ① "崩溃率>20%"无数据来源；② asyncio 三通道 + 每通道 try/except 隔离已足够（8s/10min/15min 低频轮询，非 CPU 密集）；③ 多进程引入 IPC 复杂度，个人项目得不偿失。真实问题（线程安全）已另修复 |
| 按采集源分三张表（events_window/browser/git） | 单表 + kind 字段 + meta JSON 是事件溯源规范做法；分表破坏跨通道统计（周报要聚合） |
| fcntl.flock 防 git 文件锁 | `fcntl` 是 Unix 专属模块，Windows 上不存在，代码无法运行；且本项目用 `git log` 只读子进程，不受 git gc 锁影响 |
| pywinauto/selenium 采集浏览器历史 | 项目用"复制 History 文件后读 SQLite"，比 UI 自动化方案更可靠、更轻，无需浏览器驱动 |
| WS_EX_TRANSPARENT 鼠标穿透悬浮球 | 该属性会让悬浮球完全点不到（点击落到下层窗口）；本项目悬浮球需要接收点击，且 Qt 属性已满足透明需求 |
| win10toast 通知 | 库已停止维护，Windows 11 兼容性差；Qt `QSystemTrayIcon.showMessage` 已够用 |
| ThreadPoolExecutor 重构桌面端 | 项目已用 QThread + Signal/Slot 解耦，网络请求本就不在主线程 |
| Prometheus 指标导出 | 个人项目过度设计；轻量心跳 + 日志已覆盖核心风险 |

## 四、结论

评审文档的**架构方向判断正确**（采集可靠性、记忆评分、隐私是三大核心风险），
但**约 1/3 的具体方案不适用于本项目**（错误假设、平台不兼容、或项目已实现）。
真正采纳的 6 项均为低成本高价值改动，已全部落地 v0.2。
