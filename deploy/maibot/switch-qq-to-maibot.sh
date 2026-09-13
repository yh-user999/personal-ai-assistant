#!/usr/bin/env bash
set -euo pipefail

# Switch the active NapCat OneBot route to MaiBot without storing live
# credentials or QQ/group identifiers in this repository.

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'this script must run as root\n' >&2
  exit 1
fi

: "${NAPCAT_MAIN_CONFIG:?Set NAPCAT_MAIN_CONFIG to the active NapCat config}"
: "${NAPCAT_ONEBOT_CONFIG:?Set NAPCAT_ONEBOT_CONFIG to the active OneBot config}"
: "${ASTRBOT_CONFIG:?Set ASTRBOT_CONFIG to the AstrBot plugin config}"
: "${MAIBOT_ADAPTER_CONFIG:?Set MAIBOT_ADAPTER_CONFIG to the MaiBot adapter config}"
: "${MAIBOT_NAPCAT_HOST:?Set MAIBOT_NAPCAT_HOST for the forward WebSocket}"
: "${MAIBOT_NAPCAT_PORT:?Set MAIBOT_NAPCAT_PORT for the forward WebSocket}"

MAIBOT_NAPCAT_TOKEN="${MAIBOT_NAPCAT_TOKEN:-}"
BACKUP_ROOT="${MAIBOT_SWITCH_BACKUP_ROOT:-/opt/maibot/rollback/qq-switch-$(date +%Y%m%d%H%M%S)}"

for path in "$NAPCAT_MAIN_CONFIG" "$NAPCAT_ONEBOT_CONFIG" "$ASTRBOT_CONFIG" "$MAIBOT_ADAPTER_CONFIG"; do
  [[ -f "$path" ]] || { printf 'missing required file: %s\n' "$path" >&2; exit 1; }
done

mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT"
for path in "$NAPCAT_MAIN_CONFIG" "$NAPCAT_ONEBOT_CONFIG" "$ASTRBOT_CONFIG" "$MAIBOT_ADAPTER_CONFIG"; do
  install -m 0600 "$path" "$BACKUP_ROOT/$(basename "$path")"
done

if [[ -z "$MAIBOT_NAPCAT_TOKEN" ]]; then
  MAIBOT_NAPCAT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

NAPCAT_MAIN_CONFIG="$NAPCAT_MAIN_CONFIG" \
NAPCAT_ONEBOT_CONFIG="$NAPCAT_ONEBOT_CONFIG" \
ASTRBOT_CONFIG="$ASTRBOT_CONFIG" \
MAIBOT_ADAPTER_CONFIG="$MAIBOT_ADAPTER_CONFIG" \
MAIBOT_NAPCAT_HOST="$MAIBOT_NAPCAT_HOST" \
MAIBOT_NAPCAT_PORT="$MAIBOT_NAPCAT_PORT" \
MAIBOT_NAPCAT_TOKEN="$MAIBOT_NAPCAT_TOKEN" \
BACKUP_ROOT="$BACKUP_ROOT" \
python3 - <<'PY'
import json
import os
import re
import stat
import tempfile
from pathlib import Path

NAPCAT_MAIN_CONFIG = Path(os.environ["NAPCAT_MAIN_CONFIG"])
NAPCAT_ONEBOT_CONFIG = Path(os.environ["NAPCAT_ONEBOT_CONFIG"])
ASTRBOT_CONFIG = Path(os.environ["ASTRBOT_CONFIG"])
MAIBOT_ADAPTER_CONFIG = Path(os.environ["MAIBOT_ADAPTER_CONFIG"])
HOST = os.environ["MAIBOT_NAPCAT_HOST"]
PORT = int(os.environ["MAIBOT_NAPCAT_PORT"])
TOKEN = os.environ["MAIBOT_NAPCAT_TOKEN"]
BACKUP_ROOT = os.environ["BACKUP_ROOT"]


def atomic_write(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def find_group_allowed_ids(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "group_allowed_ids":
                return child
        for child in value.values():
            result = find_group_allowed_ids(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = find_group_allowed_ids(child)
            if result is not None:
                return result
    return None


def normalize_group_ids(raw):
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        values = [str(part).strip() for part in raw if str(part).strip()]
    else:
        raise RuntimeError("AstrBot group_allowed_ids has an unsupported format")
    if any(value == "*" for value in values):
        raise RuntimeError("refusing wildcard group_allowed_ids during QQ cutover")
    return sorted(set(values))


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def set_toml_keys(lines, section: str, replacements: dict[str, str]):
    current = None
    found = set()
    output = []
    section_pattern = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    for line in lines:
        match = section_pattern.match(line)
        if match:
            current = match.group(1)
        if current == section:
            for key, replacement in replacements.items():
                if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                    output.append(f"{key} = {replacement}\n")
                    found.add(key)
                    break
            else:
                output.append(line)
        else:
            output.append(line)
    missing = set(replacements) - found
    if missing:
        raise RuntimeError(f"MaiBot adapter config keys missing in [{section}]: {sorted(missing)}")
    return output


astrbot_config = load_json(ASTRBOT_CONFIG)
group_raw = find_group_allowed_ids(astrbot_config)
group_ids = normalize_group_ids(group_raw)
if not group_ids:
    raise RuntimeError("current AstrBot group whitelist is empty; refusing an implicit scope change")

napcat_main = load_json(NAPCAT_MAIN_CONFIG)
main_onebot = napcat_main.setdefault("onebot11", {})
for item in main_onebot.setdefault("wsReverse", []):
    if item.get("name") == "astrbot":
        item["enable"] = False

napcat_onebot = load_json(NAPCAT_ONEBOT_CONFIG)
network = napcat_onebot.setdefault("network", {})
for item in network.setdefault("websocketClients", []):
    if item.get("name") == "astrbot":
        item["enable"] = False

servers = [item for item in network.setdefault("websocketServers", []) if item.get("name") != "maibot"]
servers.append(
    {
        "name": "maibot",
        "enable": True,
        "host": HOST,
        "port": PORT,
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "token": TOKEN,
        "enableForcePushEvent": True,
        "debug": False,
        "heartInterval": 30000,
    }
)
network["websocketServers"] = servers

adapter_lines = MAIBOT_ADAPTER_CONFIG.read_text(encoding="utf-8").splitlines(keepends=True)
adapter_lines = set_toml_keys(
    adapter_lines,
    "plugin",
    {"enabled": "true"},
)
adapter_lines = set_toml_keys(
    adapter_lines,
    "napcat_server",
    {
        "host": toml_string(HOST),
        "port": str(PORT),
        "token": toml_string(TOKEN),
    },
)
adapter_lines = set_toml_keys(
    adapter_lines,
    "chat",
    {
        "enable_chat_list_filter": "true",
        "group_list_type": toml_string("whitelist"),
        "group_list": json.dumps(group_ids, ensure_ascii=False),
        "private_list_type": toml_string("whitelist"),
        "private_list": "[]",
    },
)

atomic_json(NAPCAT_MAIN_CONFIG, napcat_main)
atomic_json(NAPCAT_ONEBOT_CONFIG, napcat_onebot)
atomic_write(MAIBOT_ADAPTER_CONFIG, "".join(adapter_lines))

print(f"updated QQ routing; backup={BACKUP_ROOT}; preserved_groups={len(group_ids)}")
PY

printf 'QQ routing prepared for MaiBot; backup=%s\n' "$BACKUP_ROOT"
