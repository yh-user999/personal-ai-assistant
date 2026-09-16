"""OneBot v11 消息事件的最小、fail-closed 解析器。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import unquote


_CQ_RE = re.compile(r"\[CQ:(?P<type>[a-zA-Z0-9_]+)(?P<data>(?:,[^\]]*)*)\]")
_ID_RE = re.compile(r"^\d{1,32}$")


def _id(value: Any, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        return ""
    return text if _ID_RE.fullmatch(text) else ""


def _message_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not _ID_RE.fullmatch(text):
        return ""
    return text


def _parse_cq_data(value: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for item in value.lstrip(",").split(","):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        data[key] = unescape(unquote(raw))
    return data


def _segments(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        output: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            segment_type = str(item.get("type") or "").strip().casefold()
            if not segment_type:
                continue
            raw_data = item.get("data")
            data = dict(raw_data) if isinstance(raw_data, dict) else {}
            output.append({"type": segment_type, "data": data})
        return output
    text = str(value or "")
    output: list[dict[str, Any]] = []
    cursor = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > cursor:
            output.append({"type": "text", "data": {"text": text[cursor:match.start()]}})
        output.append({"type": match.group("type").casefold(), "data": _parse_cq_data(match.group("data"))})
        cursor = match.end()
    if cursor < len(text):
        output.append({"type": "text", "data": {"text": text[cursor:]}})
    if not output and text:
        output.append({"type": "text", "data": {"text": text}})
    return output


@dataclass(frozen=True, slots=True)
class OneBotMessage:
    """只保留网关处理所需字段，不在对象里保存完整原始事件。"""

    message_type: str
    user_id: str
    group_id: str
    self_id: str
    message_id: str
    segments: tuple[dict[str, Any], ...]

    @classmethod
    def from_payload(cls, payload: Any) -> OneBotMessage | None:
        if not isinstance(payload, dict) or str(payload.get("post_type") or "").casefold() != "message":
            return None
        message_type = str(payload.get("message_type") or "").casefold()
        if message_type not in {"private", "group"}:
            return None
        user_id = _id(payload.get("user_id"))
        message_id = _message_id(payload.get("message_id"))
        if not user_id or not message_id:
            return None
        group_id = _id(payload.get("group_id")) if message_type == "group" else ""
        if message_type == "group" and not group_id:
            return None
        self_id = _id(payload.get("self_id"), required=False)
        return cls(
            message_type=message_type,
            user_id=user_id,
            group_id=group_id,
            self_id=self_id,
            message_id=message_id,
            segments=tuple(_segments(payload.get("message"))),
        )

    @property
    def text(self) -> str:
        return "".join(
            str(segment.get("data", {}).get("text") or "")
            for segment in self.segments
            if segment.get("type") == "text"
        ).strip()

    @property
    def at_targets(self) -> tuple[str, ...]:
        return tuple(
            target
            for segment in self.segments
            if segment.get("type") == "at"
            for target in (_id(segment.get("data", {}).get("qq"), required=False),)
            if target
        )

    @property
    def reply_ids(self) -> tuple[str, ...]:
        return tuple(
            reply_id
            for segment in self.segments
            if segment.get("type") == "reply"
            for reply_id in (_message_id(segment.get("data", {}).get("id")),)
            if reply_id
        )

    @property
    def has_media(self) -> bool:
        return any(segment.get("type") in {"image", "video", "record", "file"} for segment in self.segments)

    @property
    def request_id(self) -> str:
        return f"onebot-{self.message_id}"


__all__ = ["OneBotMessage"]
