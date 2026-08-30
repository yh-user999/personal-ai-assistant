# QQ 接入运维手册 —— NapCat / AstrBot / 插件

> QQ 通道（第 8 课）三个组件的部署与排障。架构：手机 QQ ↔ NapCat（容器）
> ↔ AstrBot（宿主）↔ 插件 xy ↔ 小月服务 /api/chat。

## 一、架构与数据流

```
主人手机 QQ
   ↕（私聊）
NapCat 容器（QQ 协议端，onebot HTTP :3100）
   ↕（onebot 事件）
AstrBot 宿主（插件 astrbot_plugin_xy 跑在这里）
   ↕（HTTP Bearer → http://127.0.0.1:8000/api/chat）
小月 FastAPI 服务器
```

反向通道：小月的定时提醒经 `qq_push` 服务 → NapCat onebot HTTP → 主人 QQ
（**提醒唯一通道**，手机必达）。

## 二、隐私铁律（插件白名单）

- 群聊消息：任何身份（包括主人）一律静默，零 API 调用
- 陌生私聊：静默
- 仅 `owner_qq` 配置的主私聊进小月；owner 未配置 = 全拒（fail-closed）
- 文件入库同样走此白名单：陌生人发文件连识别都不会发生

## 三、日常操作

### NapCat 扫码登录 / 掉线恢复

1. 访问 AstrBot 宿主的 NapCat WebUI（或容器日志）获取二维码：
   服务器曾临时公开放行 `/qr.png`（扫码完成后务必撤回该放行）
2. 手机 QQ 扫码登录
3. 掉线自动恢复依赖 NapCat 自身会话保持；**容器重启后可能掉登录**——
   重新扫码即可（教训见 LESSONS 6.x）

### 插件配置（AstrBot 控制台 → 插件配置）

| 项 | 说明 |
|----|------|
| owner_qq | 主人 QQ 号（纯数字字符串） |
| api_base | 小月服务地址，同机 `http://127.0.0.1:8000` |
| api_token | 与服务器 .env 的 API_TOKEN 一致 |
| onebot_http | NapCat onebot HTTP（文件会话下载通道），默认 `:3100` |
| onebot_token | NapCat onebot token |
| download_proxy | 文件 CDN 下载兜底代理（如 clash `http://127.0.0.1:7890`；直连 502 时必配） |
| container_path_map | NapCat 容器→宿主机路径映射（分号分隔 `容器=宿主` 对） |

### 服务器侧 .env（QQ 推送通道）

```
QQ_PUSH_URL=http://127.0.0.1:3100   # NapCat onebot HTTP
QQ_PUSH_TOKEN=...                    # 与 NapCat onebot token 一致
QQ_ADMIN_ID=10001                    # 主人 QQ（纯数字，非数字启动期报错）
```

## 四、排障速查

| 现象 | 排查顺序 |
|------|----------|
| QQ 发消息无回复 | ① 小月服务 health ② 插件 api_base/api_token ③ AstrBot 日志 `[xy]` 前缀 |
| 提醒不推 QQ | ① 服务器 .env 三项 QQ_* 是否齐全 ② NapCat 容器是否在线（`docker ps`）③ 手动 POST onebot `/send_private_msg` 测试 |
| 发文件无反应 | ① 是否主人账号 ② AstrBot 日志 File 组件识别 ③ `download_proxy` 是否需要（CDN 502）④ 容器路径映射是否与挂载一致 |
| 收到提醒但积压一批 | NapCat 掉线恢复后的合并摘要（24h 以上旧项合并推送，属正常设计） |
| 主人消息触发其他插件 | 插件版本 ≥ v1.2（含 stop_event），检查插件版本 |
| 群聊里机器人说话 | 严重隐私问题！确认插件版本 ≥ v1.2 且 owner_qq 已配置（群聊分支在任何 API 调用前 return） |

## 五、升级注意

- 插件目录 `qq/astrbot_plugin_xy` 改动后需同步到 AstrBot 宿主并重载插件
- 小月服务端 QQ 相关配置变更只需重启 assistant 服务
- NapCat 容器升级后重新扫码；检查 `container_path_map` 与新镜像的路径约定
