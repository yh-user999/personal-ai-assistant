#!/usr/bin/env bash
# 部署实时检索后端（SearXNG 自托管）
#
# 用途：给助手的 web_search provider 提供事实层来源。
# 设计约束：
#   - 只监听回环地址：检索后端不对外暴露，只给本机助手进程用
#   - 关闭 limiter：本实例不走公网，限流只会干扰正常的 API 调用
#   - 必须启用 json 格式：默认配置里 formats 是注释掉的，不启用则 /search?format=json 返回 403
#
# 用法:
#   bash scripts/deploy_searxng.sh
#
# 完成后在助手 .env 里配置（仅本机回环地址，勿填公网地址）：
#   SEARCH_BACKEND_URL=http://127.0.0.1:8888
#
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-searxng}"
IMAGE="${IMAGE:-searxng/searxng:latest}"
CONFIG_DIR="${CONFIG_DIR:-/opt/searxng}"
PORT="${PORT:-8888}"
BIND_ADDRESS="${BIND_ADDRESS:-127.0.0.1}"
MEMORY_LIMIT="${MEMORY_LIMIT:-512m}"
# 需要代理出网时设置，例如 http://127.0.0.1:7890；留空表示直连
OUTBOUND_PROXY="${OUTBOUND_PROXY:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 docker，请先安装 Docker Engine" >&2
  exit 1
fi

echo "==> 1/4 拉取镜像 $IMAGE"
docker pull "$IMAGE"

echo "==> 2/4 准备配置 $CONFIG_DIR/settings.yml"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/settings.yml" ]; then
  # 从镜像里取默认配置作为基线（镜像内路径随版本固定）
  tmp_id=$(docker create "$IMAGE")
  docker cp "$tmp_id:/usr/local/searxng/searx/settings.yml" "$CONFIG_DIR/settings.yml"
  docker rm "$tmp_id" >/dev/null
fi

python3 - "$CONFIG_DIR/settings.yml" <<'PY'
import secrets
import sys

import yaml

path = sys.argv[1]
text = open(path, encoding="utf-8").read()

# search 段里 formats 默认是注释掉的，启用 html + json
if "formats: [html, csv, json, rss]" in text and "  formats:\n    - json" not in text:
    anchor = "  formats:\n    - html\n\nserver:"
    if anchor in text:
        text = text.replace(anchor, "  formats:\n    - html\n    - json\n\nserver:", 1)
    else:
        raise SystemExit("未找到 search.formats 片段，请手工启用 json 格式")

# 替换镜像默认密钥
if 'secret_key: "ultrasecretkey"' in text:
    text = text.replace(
        'secret_key: "ultrasecretkey"', f'secret_key: "{secrets.token_urlsafe(48)}"', 1
    )

open(path, "w", encoding="utf-8").write(text)

config = yaml.safe_load(text)
assert "json" in config["search"]["formats"], "json 格式未启用"
assert config["server"]["secret_key"] != "ultrasecretkey", "仍在用默认密钥"
print("配置校验通过: formats=%s limiter=%s" % (config["search"]["formats"], config.get("limiter")))
PY
chmod 600 "$CONFIG_DIR/settings.yml"

echo "==> 3/4 启动容器（仅监听 $BIND_ADDRESS:$PORT）"
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# GRANIAN_HOST/PORT 必须显式覆盖：镜像内置默认值是 ::8080（全网卡），
# 只设 SEARXNG_BIND_ADDRESS 不会改变实际监听地址。
proxy_env=()
if [ -n "$OUTBOUND_PROXY" ]; then
  proxy_env=(
    -e "HTTP_PROXY=$OUTBOUND_PROXY"
    -e "HTTPS_PROXY=$OUTBOUND_PROXY"
    -e "NO_PROXY=localhost,127.0.0.1"
  )
fi

docker run -d --name "$CONTAINER_NAME" --restart unless-stopped \
  --network host \
  --memory "$MEMORY_LIMIT" \
  -e SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
  -e "SEARXNG_PORT=$PORT" -e "SEARXNG_BIND_ADDRESS=$BIND_ADDRESS" \
  -e "GRANIAN_HOST=$BIND_ADDRESS" -e "GRANIAN_PORT=$PORT" \
  "${proxy_env[@]}" \
  -v "$CONFIG_DIR/settings.yml:/etc/searxng/settings.yml:ro" \
  "$IMAGE" >/dev/null

echo "==> 4/4 验证 JSON 检索接口"
sleep 10
code=$(curl -sS -m 45 -o /tmp/searxng-verify.json -w '%{http_code}' \
  "http://$BIND_ADDRESS:$PORT/search?q=test&format=json&categories=news&time_range=week" || true)
if [ "$code" != "200" ]; then
  echo "检索接口返回 HTTP $code，请检查容器日志: docker logs $CONTAINER_NAME" >&2
  exit 1
fi
python3 - /tmp/searxng-verify.json <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
results = payload.get("results") or []
print(f"检索接口可用：返回 {len(results)} 条结果")
if not results:
    print("警告：本次查询无结果，可能是上游引擎全部不可达（检查出网与代理）")
PY
rm -f /tmp/searxng-verify.json

echo
echo "完成。请在助手 .env 配置：SEARCH_BACKEND_URL=http://$BIND_ADDRESS:$PORT"
