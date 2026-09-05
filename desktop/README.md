# Desktop —— PySide6 桌面悬浮球

桌面角落的半透明机器人图标（可拖拽），点击展开聊天气泡面板，托盘常驻。文本消息走 `/api/chat` JSON；带图片的消息走 `/api/chat/vision` multipart。

## 功能

| 组件 | 文件 | 说明 |
|---|---|---|
| 悬浮球 | `floating_ball.py` | 无边框透明圆形图标，可拖拽，点击展开 |
| 聊天面板 | `chat_panel.py` | 气泡聊天窗、Markdown 展示、状态灯联动、选图、剪贴板粘贴和图片-only |
| 图片入口 | `chat_panel.py` | “图片”打开文件选择器；“粘贴”或 `Ctrl+V` 接收剪贴板截图/图片 |
| 图片上传 | `api_client.py` | 图片只读打开，发送 multipart 到 `/api/chat/vision`；文本保持 JSON `/api/chat` |
| 后台线程 | `chat_workers.py` | 网络请求在 QThread 中执行，图片请求不阻塞 UI，也不进入本地执行器 |
| 临时文件 | `chat_panel.py` | 剪贴板图片保存为临时 PNG；发送完成、失败、取消后清理，用户原图不修改 |
| 小说工作台 | `api_client.py`、`ssh_tunnel.py` | 点击入口后自动建立/复用 SSH 隧道，再用系统默认浏览器打开 `/novel/` |
| 托盘 | `tray.py` | 系统托盘：打开面板/小说工作台/今日概览/周报/退出 |

### 图片交互约定

- 支持 JPEG、PNG、WebP；桌面侧先检查扩展名、可读性和 10MB 本地大小上限，服务端再做 MIME/文件头权威校验。
- 可只发图片；图片-only 直接提交视觉请求，跳过本地快捷执行器。
- 图片 + 文字时，文字作为视觉问题；空文字由服务端使用默认识图指令。
- 上传字段包含 `image`、`message`、`request_id`；桌面端不发送 QQ 身份头。
- 剪贴板图片会写入进程临时目录，原始文件选择路径只读打开，不由客户端删除。

## 运行

```bash
pip install -r requirements.txt
python main.py
```

环境变量：

```dotenv
# 直连 API；如果配置了 NOVEL_TUNNEL_TARGET，小说网页会优先走本机 SSH 隧道
SERVER_URL=http://<服务器私网地址>:8000
API_TOKEN=<与服务端角色配置对应的 token>

# 小说工作台自动 SSH 隧道（推荐使用 SSH config alias）
NOVEL_TUNNEL_TARGET=<ssh-user>@<ssh-host>
NOVEL_TUNNEL_LOCAL_PORT=18000
NOVEL_TUNNEL_REMOTE_HOST=127.0.0.1
NOVEL_TUNNEL_REMOTE_PORT=8000
NOVEL_TUNNEL_IDENTITY_FILE=<可选私钥路径>
# 可选完整地址；填写后覆盖自动生成的本机地址
NOVEL_WEB_URL=
```

### 点击小说按钮自动建立隧道

桌面端的悬浮球右键菜单、聊天面板“✒ 小说”按钮和系统托盘菜单共用同一后台流程。配置 `NOVEL_TUNNEL_TARGET` 后，点击任意入口会在后台调用 Windows 自带 OpenSSH：

1. 使用 `ssh -N -T -L 本机端口:远端回环地址:远端端口 目标` 建立转发；
2. 轮询确认 `127.0.0.1:<本机端口>` 已监听后，再打开系统默认浏览器；
3. 如果本机端口已有可用转发则直接复用，不会重复启动；机器人退出时只关闭本进程创建的隧道。

首次使用前，请在 Windows 的 SSH config 或密钥中完成一次认证准备（推荐配置 `Host`、`IdentityFile` 和已知主机）。日常点击按钮不需要手动执行 `ssh -L`，也不需要把 API token 放进网页 URL；网页继续使用自身的 Token 输入框。

如果未配置 `NOVEL_TUNNEL_TARGET`，且没有填写 `NOVEL_WEB_URL`，桌面端会回退到 `SERVER_URL/novel/`。模板中的 `<ssh-user>`、`<ssh-host>` 和私钥路径均为脱敏占位符，请勿替换后提交真实值。

## 打包 exe

```powershell
# 见 scripts/build_desktop.ps1
pyinstaller --noconfirm --onefile --windowed main.py
```

## 实现状态

- [x] 悬浮球、聊天面板、托盘和后台网络线程
- [x] 文本 `/api/chat` JSON 请求与 request_id
- [x] 选图、剪贴板粘贴、图片-only 和多 multipart 上传 `/api/chat/vision`
- [x] 图片扩展名/大小/可读性检查、临时文件清理、图片请求跳过本地执行器
- [x] 服务端错误通过聊天流回显，完成后机器人状态灯恢复在线

## 图片专项测试证据

`pytest desktop/tests/test_image_input.py -q`：**6 passed**，覆盖 JSON 文本、multipart 图片、选图、剪贴板临时文件、QThread 传递图片路径和图片-only 分支。
