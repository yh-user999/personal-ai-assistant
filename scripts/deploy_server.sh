#!/usr/bin/env bash
# 服务端部署脚本（JD 服务器，Ubuntu/Debian 示例）
# 用法: bash scripts/deploy_server.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/personal-ai-assistant}"
SERVICE_NAME="personal-assistant"
SERVICE_USER="${SERVICE_USER:-paa}"
SERVICE_GROUP="${SERVICE_GROUP:-paa}"

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "==> 1/6 创建服务账号和目录"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  $SUDO useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
fi
$SUDO install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR"

echo "==> 2/6 拉取代码"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone git@github.com:yh-user999/personal-ai-assistant.git "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

echo "==> 3/6 安装依赖（固定使用项目 venv）"
$SUDO install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$APP_DIR/server/data" "$APP_DIR/server/logs"
$SUDO python3 -m venv "$APP_DIR/server/.venv"
$SUDO "$APP_DIR/server/.venv/bin/python" -m pip install -r "$APP_DIR/server/requirements.txt"

echo "==> 4/6 配置 .env（如不存在）"
if [ ! -f "$APP_DIR/.env" ]; then
  $SUDO cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  $SUDO chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/.env"
  $SUDO chmod 640 "$APP_DIR/.env"
  echo "!!! 请编辑 $APP_DIR/.env 填入 LLM_API_KEY / EMBEDDING_API_KEY"
fi
$SUDO chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/server/data" "$APP_DIR/server/logs"

echo "==> 5/6 注册 systemd 服务"
$SUDO tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Personal AI Assistant FastAPI Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$APP_DIR/server
ExecStart=$APP_DIR/server/.venv/bin/python run.py
EnvironmentFile=$APP_DIR/.env
UMask=027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/server/data $APP_DIR/server/logs
Restart=on-failure
RestartSec=5
MemoryMax=1G
TasksMax=128
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
EOF

echo "==> 6/6 启用服务"

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now ${SERVICE_NAME}
$SUDO systemctl status ${SERVICE_NAME} --no-pager
echo "==> 完成。访问 http://<服务器IP>:8000"
