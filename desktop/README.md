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
| 托盘 | `tray.py` | 系统托盘：打开面板/今日概览/周报/退出 |

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
SERVER_URL=http://<服务器私网地址>:8000
API_TOKEN=<与服务端角色配置对应的 token>
```

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
