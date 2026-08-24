# 部署环境记录

> 项目部署的服务器与网络信息。**JD 服务器公网 IP：117.72.11.59**
> （注意：101.33.229.73 不是本项目服务器，访问它出现 502 与项目无关）

## 服务器

| 项 | 值 |
|---|---|
| 公网 IP | **117.72.11.59**（京东云 ECS） |
| 内网 IP | 172.16.0.8（eth0） |
| 系统 | Ubuntu 24.04.4 LTS，x86_64 |
| 配置 | 4 核 Xeon Gold 6148 / 8G 内存 / 177G 磁盘 |
| Python | 3.12.3（系统包受 PEP 668 限制，必须用 venv） |
| SSH 别名 | `jd` / `jd-cloud`（见 ~/.ssh/config，密钥 jd_cloud_ed25519） |
| 项目路径 | `/root/personal-ai-assistant` |

## 服务规划

| 端口 | 服务 | 状态 |
|---|---|---|
| 8000 | 本项目 FastAPI（uvicorn） | 运行中 |
| 7890/7891 | mihomo（clash，本机已有） | 运行中 |
| 22 | SSH | 运行中 |

## LLM/Embedding 配置（2026-08-24 确定）

| 项 | 配置 |
|---|---|
| LLM 网关 | OpenCode Zen：`https://opencode.ai/zen/v1`（OpenAI 兼容） |
| LLM 模型 | `deepseek-v4-flash`（备选 `deepseek-v4-pro`；注意 Zen 账户需有余额） |
| Embedding | 智谱：`https://open.bigmodel.cn/api/paas/v4`，模型 `embedding-3`，维度 **2048** |
| 备份 LLM | 智谱 GLM：`glm-4-flash`（同智谱 key），余额不足时临时顶替 |
| .env 备份 | `.env.bak`（原始配置快照） |

> 教训：DeepSeek 官方 key 与 OpenCode Zen key 不通用；OpenCode Zen 的
> DeepSeek 走 `opencode.ai/zen` 网关，模型名是 `deepseek-v4-*`。

## 防火墙（两层，都放行才通）

1. **系统层 ufw**（已 active）：放行 22 / 7890 / 7891 / 8000
2. **云层安全组**（京东云控制台）：入站放行 TCP 8000

## 本机已有服务（勿动）

- `/opt/QQ/qq` + Xvfb：QQ 机器人（图形协议）
- mihomo：代理（7890）
- fail2ban、docker（dockerd）

## 排障经验（2026-08-24）

- 现象：本机 `curl 127.0.0.1:8000` 通，外网访问不通/502
- 原因一：ufw 只放行了 22 → `ufw allow 8000/tcp`
- 原因二：访问了错误的公网 IP（101.33.229.73 ≠ 本机）
- 排障顺序：① 本机 curl ② `ufw status`/`iptables -L` ③ 云控制台安全组/ACL ④ 确认公网 IP 是否正确
