#!/usr/bin/env bash
set -euo pipefail

# Start a clean MaiBot core beside the existing AstrBot/NapCat stack.
# Runtime credentials are intentionally supplied by the caller and are not
# stored in this repository.

MAIBOT_ROOT="${MAIBOT_ROOT:-/opt/maibot}"
MAIBOT_IMAGE="${MAIBOT_IMAGE:-sengokucola/maibot:latest}"
CONTAINER_NAME="${MAIBOT_CONTAINER_NAME:-maim-bot-core}"
NETWORK_MODE="${MAIBOT_NETWORK_MODE:-bridge}"
WEBUI_PORT="${MAIBOT_WEBUI_PORT:-18001}"

: "${MAIBOT_EULA_AGREE:?Set MAIBOT_EULA_AGREE for this launch}"
: "${MAIBOT_PRIVACY_AGREE:?Set MAIBOT_PRIVACY_AGREE for this launch}"
: "${MAIBOT_WEBUI_HOST:?Set MAIBOT_WEBUI_HOST for this launch}"

case "$NETWORK_MODE" in
  host)
    NETWORK_ARGS=(--network host)
    ;;
  bridge)
    : "${MAIBOT_PUBLISH_SPEC:?Set MAIBOT_PUBLISH_SPEC for bridge mode}"
    NETWORK_ARGS=(--publish "$MAIBOT_PUBLISH_SPEC")
    ;;
  *)
    printf 'unsupported network mode: %s\n' "$NETWORK_MODE" >&2
    exit 2
    ;;
esac

for dir in \
  "$MAIBOT_ROOT/docker-config/mmc" \
  "$MAIBOT_ROOT/data/MaiMBot" \
  "$MAIBOT_ROOT/data/MaiMBot-plugin-data" \
  "$MAIBOT_ROOT/data/MaiMBot/emoji" \
  "$MAIBOT_ROOT/data/MaiMBot/plugins" \
  "$MAIBOT_ROOT/data/MaiMBot/logs" \
  "$MAIBOT_ROOT/depends-data"; do
  mkdir -p "$dir"
done

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  printf 'container already exists: %s\n' "$CONTAINER_NAME" >&2
  exit 2
fi

docker run --detach \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --pull missing \
  "${NETWORK_ARGS[@]}" \
  --env "TZ=Asia/Shanghai" \
  --env "EULA_AGREE=${MAIBOT_EULA_AGREE}" \
  --env "PRIVACY_AGREE=${MAIBOT_PRIVACY_AGREE}" \
  --env "MAIBOT_LEGACY_0X_UPGRADE_CONFIRMED=1" \
  --env "MAIBOT_STATISTICS_REPORT_PATH=/MaiMBot/data/maibot_statistics.html" \
  --env "WEBUI_HOST=${MAIBOT_WEBUI_HOST}" \
  --env "WEBUI_PORT=${WEBUI_PORT}" \
  --volume "$MAIBOT_ROOT/docker-config/mmc:/MaiMBot/config" \
  --volume "$MAIBOT_ROOT/data/MaiMBot:/MaiMBot/data" \
  --volume "$MAIBOT_ROOT/data/MaiMBot-plugin-data:/MaiMBot/data/plugins" \
  --volume "$MAIBOT_ROOT/data/MaiMBot/emoji:/data/emoji" \
  --volume "$MAIBOT_ROOT/data/MaiMBot/plugins:/MaiMBot/plugins" \
  --volume "$MAIBOT_ROOT/data/MaiMBot/logs:/MaiMBot/logs" \
  --volume "$MAIBOT_ROOT/depends-data:/MaiMBot/depends-data" \
  "$MAIBOT_IMAGE"

printf 'started %s in %s network mode using %s\n' \
  "$CONTAINER_NAME" "$NETWORK_MODE" "$MAIBOT_IMAGE"
