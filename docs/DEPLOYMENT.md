# 部署环境记录

> 项目部署的服务器与网络信息。**注意脱敏：本文档不含公网 IP 与任何密钥**。
> 访问方式：Tailscale 专线（见下），公网 8000 已关闭。

## 服务器

| 项 | 值 |
|---|---|
| 平台 | 京东云 ECS（公网 IP 见本地笔记，不入库） |
| 系统 | Ubuntu 24.04.4 LTS，x86_64 |
| 配置 | 4 核 Xeon Gold 6148 / 8G 内存 / 177G 磁盘 |
| Python | 3.12.3（系统包受 PEP 668 限制，必须用 venv） |
| SSH | 密钥认证 + fail2ban（22 端口为唯一公网暴露） |
| 项目路径 | `/root/personal-ai-assistant` |
| Tailscale | 已部署（v1.x），主机名 `jd-clash` |

## 网络架构（2026-08-25 起）

```
Windows / 手机 ──Tailscale 加密专线──→ JD 服务器（100.x.y.z:8000）
公网：仅 SSH 22 暴露；8000 已从 ufw + 安全组双移除
```

**访问规则**：
- 服务地址一律用 Tailscale IP（`100.x.y.z:8000`），不使用公网 IP
- 所有 API 请求带 `Authorization: Bearer <API_TOKEN>`（.env 配置，不入库）
- 换设备访问 = 该设备登录同一 Tailscale 账号即可

## 服务规划

| 端口 | 服务 | 暴露面 |
|---|---|---|
| 8000 | 本项目 FastAPI（uvicorn） | 仅 Tailscale 专线 |
| 7890/7891 | mihomo（clash，本机已有） | 公网（原有配置） |
| 22 | SSH | 公网（密钥 + fail2ban） |

## LLM/Embedding 配置（2026-08-24 确定，已验证可用）

| 项 | 配置 |
|---|---|
| LLM 网关 | **OpenCode Go 订阅**：`https://opencode.ai/zen/go/v1`（OpenAI 兼容） |
| LLM 模型 | `deepseek-v4-flash`（Go 套餐模型列表内，另有 `deepseek-v4-pro`） |
| Embedding | 智谱：`https://open.bigmodel.cn/api/paas/v4`，模型 `embedding-3`，维度 **2048** |
| API 鉴权 | `API_TOKEN`（32 字节随机，服务器/采集器/桌面端共用，不入库） |

> 教训：OpenCode 有两个网关——Zen（按量计费 `/zen/v1`）与 Go（$10/月订阅
> `/zen/go/v1`）；key 相同但端点不同。DeepSeek 官方 key 与 OpenCode key 不通用，
> Go 套餐里 DeepSeek 的模型 ID 是 `deepseek-v4-*`。

## 防火墙（三层）

1. **Tailscale 接口**：`ufw allow in on tailscale0`（专线全放行）
2. **系统层 ufw**：公网仅 22 / 7890 / 7891；8000 已移除
3. **云层安全组**：入站 8000 规则已删除

## 本机已有服务（勿动）

- `/opt/QQ/qq` + Xvfb：QQ 机器人（图形协议）
- mihomo：代理（7890）
- fail2ban、docker（dockerd）

## 排障经验（2026-08-24）

- 现象：本机 `curl 127.0.0.1:8000` 通，外网访问不通/502
- 原因一：ufw 只放行了 22 → `ufw allow 8000/tcp`（后改为 Tailscale 方案）
- 原因二：访问了错误的公网 IP（曾误用另一台机器 IP 导致 502）
- 排障顺序：① 本机 curl ② `ufw status`/`iptables -L` ③ 云控制台安全组/ACL ④ 确认 IP 是否正确
- 安全演进：裸奔 → API_TOKEN 鉴权 → Tailscale 关闭公网 8000（四层防御见 LESSONS.md）
