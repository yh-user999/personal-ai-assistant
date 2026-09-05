"""受控图片载荷校验；只在请求内存中保留 data URL，不抓取 URL。"""
from __future__ import annotations

import base64
import hashlib
import zlib
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedImage:
    media_type: str
    sha256: str
    size: int
    data_url: str


def _error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _looks_like_jpeg(data: bytes) -> bool:
    if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        return False
    i = 2
    saw_segment = False
    while i < len(data) - 2:
        if data[i] != 0xFF:
            return False
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            return False
        marker = data[i]
        i += 1
        if marker == 0xDA:  # SOS 后是熵编码数据，直接确认末尾 EOI。
            return data.rfind(b"\xff\xd9") == len(data) - 2 and saw_segment
        if marker in {0xD8, 0xD9}:
            return False
        if i + 2 > len(data):
            return False
        segment_size = int.from_bytes(data[i : i + 2], "big")
        if segment_size < 2 or i + segment_size > len(data):
            return False
        i += segment_size
        saw_segment = True
    return saw_segment and data[-2:] == b"\xff\xd9"


def _looks_like_png(data: bytes) -> bool:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    saw_ihdr = False
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + size
        if end > len(data):
            return False
        payload = data[offset + 8 : offset + 8 + size]
        stored_crc = int.from_bytes(data[offset + 8 + size : end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != stored_crc:
            return False
        if kind == b"IHDR":
            if saw_ihdr or size != 13:
                return False
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            if width <= 0 or height <= 0:
                return False
            saw_ihdr = True
        if kind == b"IEND":
            return saw_ihdr and size == 0 and end == len(data)
        offset = end
    return False


def _looks_like_webp(data: bytes) -> bool:
    if not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        return False
    if int.from_bytes(data[4:8], "little") != len(data) - 8 or len(data) < 24:
        return False
    chunk_size = int.from_bytes(data[16:20], "little")
    return data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"} and 20 + chunk_size <= len(data)


def _detect_magic(data: bytes) -> str | None:
    """仅按文件头识别候选格式，供 MIME 不一致时返回 415。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _detect_header(data: bytes) -> str | None:
    detected = _detect_magic(data)
    if detected == "image/jpeg" and _looks_like_jpeg(data):
        return detected
    if detected == "image/png" and _looks_like_png(data):
        return detected
    if detected == "image/webp" and _looks_like_webp(data):
        return detected
    return None


async def validate_upload(upload: Any, *, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> ValidatedImage:
    """读取并校验 multipart 图片，使用 max+1 读取判断超限。"""
    declared = str(getattr(upload, "content_type", "") or "").split(";", 1)[0].strip().lower()
    if declared not in ALLOWED_MEDIA_TYPES:
        raise _error(415, "仅支持 JPEG、PNG 或 WebP 图片")
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_IMAGE_BYTES
    if limit <= 0:
        raise _error(500, "图片大小配置无效")

    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise _error(413, f"图片超过大小限制（最大 {limit} 字节）")
    if not data:
        raise _error(400, "图片文件为空")

    magic_type = _detect_magic(data)
    if magic_type is None:
        raise _error(400, "图片文件头无效或文件已损坏")
    if magic_type != declared:
        raise _error(415, "声明的图片 MIME 与文件头不一致")
    if _detect_header(data) is None:
        raise _error(400, "图片文件头无效或文件已损坏")
    detected = magic_type

    digest = hashlib.sha256(data).hexdigest()
    encoded = base64.b64encode(data).decode("ascii")
    return ValidatedImage(
        media_type=detected,
        sha256=digest,
        size=len(data),
        data_url=f"data:{detected};base64,{encoded}",
    )
