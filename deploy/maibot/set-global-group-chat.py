#!/usr/bin/env python3
"""将 MaiBot NapCat adapter 切换为“群聊空黑名单、私聊继续白名单”。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(
    "/opt/maibot/data/MaiMBot/plugins/MaiBot-Napcat-Adapter/config.toml"
)
DEFAULT_BACKUP_DIR = Path("/opt/maibot/rollback")
TARGET_SECTION = "chat"
TARGET_VALUES = {
    "group_list_type": json.dumps("blacklist", ensure_ascii=False),
    "group_list": "[]",
}
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_KEY_RE = re.compile(r"^(\s*)(group_list_type|group_list)\s*=")


class ConfigError(RuntimeError):
    """运行时配置不符合预期。"""


def _newline_for(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n"


def rewrite_group_policy(text: str) -> str:
    """只改 [chat] 的群名单策略，其他文本保持不变。"""
    lines = text.splitlines(keepends=True)
    current_section: str | None = None
    found: set[str] = set()
    output: list[str] = []

    for line in lines:
        section_match = _SECTION_RE.match(line.rstrip("\r\n"))
        if section_match:
            current_section = section_match.group(1).strip()

        key_match = _KEY_RE.match(line) if current_section == TARGET_SECTION else None
        if key_match:
            key = key_match.group(2)
            if key in found:
                raise ConfigError(f"[chat] 中重复出现配置键: {key}")
            found.add(key)
            indent = key_match.group(1)
            output.append(f"{indent}{key} = {TARGET_VALUES[key]}{_newline_for(line)}")
        else:
            output.append(line)

    missing = set(TARGET_VALUES) - found
    if missing:
        raise ConfigError(f"[chat] 缺少配置键: {sorted(missing)}")
    return "".join(output)


def _load(text: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("TOML 根节点不是对象")
    return data


def _chat(data: dict[str, Any]) -> dict[str, Any]:
    chat = data.get(TARGET_SECTION)
    if not isinstance(chat, dict):
        raise ConfigError("缺少 [chat] 配置段")
    return chat


def validate_group_policy(text: str) -> dict[str, Any]:
    """校验全群策略，同时确保私聊过滤仍保持启用。"""
    data = _load(text)
    chat = _chat(data)
    if chat.get("enable_chat_list_filter") is not True:
        raise ConfigError("拒绝继续：enable_chat_list_filter 必须保持 true")
    if chat.get("group_list_type") != "blacklist":
        raise ConfigError("group_list_type 必须为 blacklist")
    if chat.get("group_list") != []:
        raise ConfigError("group_list 必须为空列表")
    if chat.get("private_list_type") != "whitelist":
        raise ConfigError("private_list_type 必须保持 whitelist")
    if chat.get("private_list") != []:
        raise ConfigError("private_list 必须保持当前空白名单")
    return data


def summarize(text: str) -> str:
    """只输出脱敏配置摘要，不输出群号、私聊 ID 或其他值。"""
    chat = _chat(_load(text))
    group_list = chat.get("group_list")
    private_list = chat.get("private_list")
    group_count = len(group_list) if isinstance(group_list, list) else -1
    private_count = len(private_list) if isinstance(private_list, list) else -1
    return (
        f"filter_enabled={chat.get('enable_chat_list_filter')!s} "
        f"group_mode={chat.get('group_list_type')!s} "
        f"group_count={group_count} "
        f"private_mode={chat.get('private_list_type')!s} "
        f"private_count={private_count}"
    )


def _backup(path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"maibot-adapter-config-{stamp}-{os.getpid()}.toml"
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    return target


def _atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--check", action="store_true", help="只检查并输出脱敏摘要，不写入")
    args = parser.parse_args()

    config_path: Path = args.config
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在: {config_path}")

    original = config_path.read_text(encoding="utf-8")
    if args.check:
        print(summarize(original))
        return 0

    original_data = _load(original)
    original_chat = _chat(original_data)
    if original_chat.get("enable_chat_list_filter") is not True:
        raise SystemExit("拒绝修改：当前 enable_chat_list_filter 不是 true")
    if original_chat.get("private_list_type") != "whitelist":
        raise SystemExit("拒绝修改：当前 private_list_type 不是 whitelist")

    updated = rewrite_group_policy(original)
    validate_group_policy(updated)
    if updated == original:
        print("配置已经是群聊空黑名单；未写入")
        print(summarize(updated))
        return 0

    backup_path = _backup(config_path, args.backup_dir)
    _atomic_write(config_path, updated)
    print(f"已更新 MaiBot 群聊策略；运行时备份={backup_path}")
    print(summarize(updated))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        raise SystemExit(f"配置检查失败: {exc}") from exc
