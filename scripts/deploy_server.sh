#!/usr/bin/env bash
# 服务端部署脚本（JD 服务器，Ubuntu/Debian 示例）
# 用法: bash scripts/deploy_server.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/personal-ai-assistant}"
SERVICE_NAME="personal-assistant"

echo "==> 1/4 拉取代码"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone git@github.com:yh-user999/personal-ai-assistant.git "$APP_DIR"
else
  git -C "$APP_DIR" pull
fi

echo "==> 2/4 安装依赖"
cd "$APP_DIR/server"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

echo "==> 3/4 配置 .env（如不存在）"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "!!! 请编辑 $APP_DIR/.env 填入 LLM_API_KEY / EMBEDDING_API_KEY"
fi

echo "==> 4/4 注册 systemd 服务"
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Personal AI Assistant
After=network.target

[Service]
WorkingDirectory=$APP_DIR/server
ExecStart=$APP_DIR/server/.venv/bin/python run.py
Restart=always
RestartSec=5
EnvironmentFile=$APP_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE_NAME}
sudo systemctl status ${SERVICE_NAME} --no-pager
echo "==> 完成。访问 http://<服务器IP>:8000"
