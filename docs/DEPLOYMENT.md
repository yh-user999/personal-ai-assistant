# 部署环境记录

> 本文以 **2026-09-05** 已核对的实例状态为准。本文档已脱敏，不含公网 IP、真实 token、HMAC secret 或 QQ 号。
> 当前 FastAPI 服务端是手工进程启动；`scripts/deploy_server.sh` 里的 systemd 仅是新装模板，不代表当前实例已经由 systemd 接管。

## 服务器

| 项 | 值 |
|---|---|
| 平台 | 京东云 ECS（公网 IP 见本地笔记，不入库） |
| 系统 | Ubuntu 24.04.4 LTS，x86_64 |
| 配置 | 4 核 Xeon Gold 6148 / 8G 内存 / 177G 磁盘 |
| Python | 3.12.3（系统包受 PEP 668 限制，必须用 venv） |
| SSH | 密钥认证 + fail2ban（22 端口为唯一公网暴露） |
| 项目路径 | `/root/personal-ai-assistant` |
| 当前服务目录 | `/root/personal-ai-assistant/server` |
| 当前启动方式 | 手工进程：`nohup .venv/bin/python run.py > /tmp/assistant.log 2>&1 &` |
| 当前日志 | `/tmp/assistant.log` |
| Tailscale | 已部署（v1.x），主机名 `jd-clash` |

> 当前实例没有以 systemd unit 作为事实依据；需要迁移到 systemd 时，另按部署脚本评估权限、数据目录和回滚方案。

## 网络架构（2026-08-25 起）

```
Windows / 手机 ──Tailscale 加密专线──→ JD 服务器（100.x.y.z:8000）
公网：仅 SSH 22 暴露；8000 已从 ufw + 安全组双移除
```

**访问规则**：
- 服务地址一律使用 Tailscale IP（`100.x.y.z:8000`），不使用公网 IP。
- 配置 token 后，API 请求带 `Authorization: Bearer <脱敏占位符>`；真实值只放 `.env`。
- 换设备访问 = 该设备登录同一 Tailscale 账号并完成本地 token 配置。

## Windows 端部署（2026-08-25）

| 项 | 配置 |
|---|---|
| 项目路径 | `F:\Projects\git\personal-ai-assistant`（git clone，`git pull` 更新） |
| Python | 3.14（`pythonw.exe` 无窗口启动） |
| 开机自启 | 任务计划 `PAA-Collector`（登录+30s）+ `PAA-Robot`（登录+15s）；脚本 `scripts/install_autostart.ps1` |
| 崩溃自愈 | 采集器重启 3 次 / 机器人 1 次 |
| 网络依赖 | Tailscale `--unattended`（登录前自动连专线，机器人启动不红灯） |
| 日志 | 采集器 `collector/logs/collector.log`；服务端 `/tmp/assistant.log` |
| 采集范围 | 前台窗口（8s）/ Chrome+Edge 历史（10min）/ git 指定仓库（15min），本地脱敏后推送 |

## 服务规划

| 端口 | 服务 | 暴露面 |
|---|---|---|
| 8000 | 本项目 FastAPI（uvicorn） | 仅 Tailscale 专线 |
| 7890/7891 | mihomo（clash，本机已有） | 公网（原有配置） |
| 22 | SSH | 公网（密钥 + fail2ban） |

## LLM/Embedding 配置（2026-09-05 已核对）

| 项 | 配置 |
|---|---|
| LLM 网关 | **OpenCode Go 订阅**：`https://opencode.ai/zen/go/v1`（OpenAI 兼容） |
| 普通聊天模型 | `deepseek-v4-flash`，由 `LLM_MODEL` 控制 |
| 图片识别模型 | `deepseek-v4-flash-vision-exp`，由 `VISION_LLM_MODEL` 控制，仅用于 `/api/chat/vision` |
| Embedding | 智谱：`https://open.bigmodel.cn/api/paas/v4`，模型 `embedding-3`，维度 **2048** |
| LLM Key | 推荐 `LLM_API_KEYS` 以逗号或换行配置多 Key；留空时回退 `LLM_API_KEY` |
| API 鉴权 | `API_TOKEN` 或按角色拆分的 token；真实值不入库 |

> 普通聊天模型与视觉模型是两条明确配置，不要把 `LLM_MODEL` 的值误当成图片识别模型。视觉请求默认超时 90 秒，单图默认上限 10MB。

### 视觉、多 Key 与 QQ 鉴权配置（脱敏）

服务器 `.env` 只保留脱敏占位符示例：

```dotenv
LLM_API_KEYS=<key-1>,<key-2>
LLM_API_KEY=
LLM_MODEL=deepseek-v4-flash
VISION_LLM_MODEL=deepseek-v4-flash-vision-exp
VISION_MAX_IMAGE_BYTES=10485760
VISION_TIMEOUT=90

# QQ 入站插件调用 /api/chat 与 /api/chat/vision
QQ_API_TOKEN=<qq-api-token>
QQ_IDENTITY_SECRET=<shared-hmac-secret>
QQ_IDENTITY_MAX_AGE_SECONDS=300

# QQ 出站提醒到 NapCat；与 QQ_API_TOKEN 是两条不同链路
QQ_PUSH_URL=http://127.0.0.1:3100
QQ_PUSH_TOKEN=<napcat-onebot-token>
QQ_ADMIN_ID=<owner-qq-id>
```

- `LLM_API_KEYS` 最多 8 个，服务端只在日志/诊断中记录序号或脱敏指纹，不输出原文。
- `QQ_API_TOKEN` 只认证 AstrBot 插件；插件配置的 `api_token` 应填同一值，而不是把入站鉴权与出站 `QQ_PUSH_TOKEN` 混用。
- `QQ_IDENTITY_SECRET` 与插件配置的 `identity_secret` 必须一致，用于签名 QQ 号、时间戳和 `request_id`；缺失或过期时服务端 fail-closed。
- `QQ_ADMIN_ID` 只用于服务器定时提醒推送，必须是纯数字；本文不记录真实 QQ 号。

## `/api/chat/vision` 部署后检查

图片接口为 `POST /api/chat/vision`，接收 `multipart/form-data`：`image` 必填，`message` 可选，`request_id` 必填，`user_id` 可选。只接受 JPEG/PNG/WebP，服务端会按文件头、MIME 和 `VISION_MAX_IMAGE_BYTES` 校验。

部署或重启后按以下顺序检查，均不触发真实视觉调用：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/ready

cd /root/personal-ai-assistant/server
.venv/bin/python - <<'PY'
from app.main import app
assert any(getattr(route, "path", "") == "/api/chat/vision" for route in app.routes)
print("/api/chat/vision route registered")
PY
```

检查要点：

1. `/api/health` 返回 `status=ok`；`/api/ready` 的 database、scheduler、LLM 配置检查不能出现 `failed`。
2. 手工启动后查看 `/tmp/assistant.log`，确认没有配置校验错误；不要在日志或命令历史里回显 token/secret。
3. 核对普通模型、视觉模型、10MB 上限和 90 秒超时来自当前 `.env`；多 Key 只核对数量与脱敏指纹。
4. 不把真实图片请求放进健康检查。若要做一次性端到端验收，应由入口验收清单单独记录，不在日常启停脚本里重复调用外部视觉服务。
5. QQ 入口还要核对插件 `api_token` ↔ `QQ_API_TOKEN`、`identity_secret` ↔ `QQ_IDENTITY_SECRET`，以及 HMAC `request_id` 与 multipart 表单值一致。

## 防火墙（三层）

1. **Tailscale 接口**：`ufw allow in on tailscale0`（专线全放行）。
2. **系统层 ufw**：公网仅 22 / 7890 / 7891；8000 已移除。
3. **云层安全组**：入站 8000 规则已删除。

## 本机已有服务（勿动）

- `/opt/QQ/qq` + Xvfb：QQ 机器人（图形协议）。
- mihomo：代理（7890）。
- fail2ban、docker（dockerd）。

## 排障经验（2026-08-24）

- 现象：本机 `curl 127.0.0.1:8000` 通，外网访问不通/502。
- 原因一：ufw 只放行了 22 → `ufw allow 8000/tcp`（后改为 Tailscale 方案）。
- 原因二：访问了错误的公网 IP（曾误用另一台机器 IP 导致 502）。
- 排障顺序：① 本机 curl ② `ufw status`/`iptables -L` ③ 云控制台安全组/ACL ④ 确认 IP 是否正确。
- 安全演进：裸奔 → API_TOKEN 鉴权 → Tailscale 关闭公网 8000（四层防御见 LESSONS.md）。
