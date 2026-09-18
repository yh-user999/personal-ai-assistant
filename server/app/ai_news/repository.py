"""每日 AI 资讯日报的 SQLite 持久化边界。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.identity import normalize_user_id
from app.models.database import db_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AINewsRepository:
    """按主体与日期保存日报，避免重复生成和跨用户读取。"""

    def begin(self, user_id: str | None, digest_date: str, *, force: bool = False) -> dict[str, Any]:
        uid = normalize_user_id(user_id)
        now = _now()
        with db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM ai_news_digests WHERE user_id=? AND digest_date=?",
                (uid, digest_date),
            ).fetchone()
            if row and not force:
                status = str(row["status"] or "")
                if status == "ready":
                    return {"skipped": True, "reason": "日报已存在", "row": dict(row)}
                if status == "generating":
                    return {"skipped": True, "reason": "日报正在生成", "row": dict(row)}
            if row:
                conn.execute(
                    "UPDATE ai_news_digests SET status='generating', error='', updated_at=? "
                    "WHERE user_id=? AND digest_date=?",
                    (now, uid, digest_date),
                )
            else:
                conn.execute(
                    "INSERT INTO ai_news_digests "
                    "(user_id, digest_date, status, content, sources_json, stats_json, error, created_at, updated_at) "
                    "VALUES (?, ?, 'generating', '', '[]', '{}', '', ?, ?)",
                    (uid, digest_date, now, now),
                )
        return {"skipped": False, "user_id": uid, "digest_date": digest_date}

    def save_ready(
        self,
        user_id: str | None,
        digest_date: str,
        content: str,
        sources: list[dict[str, Any]],
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        uid = normalize_user_id(user_id)
        now = _now()
        with db_connection() as conn:
            conn.execute(
                "UPDATE ai_news_digests SET status='ready', content=?, sources_json=?, stats_json=?, "
                "error='', updated_at=? WHERE user_id=? AND digest_date=?",
                (
                    content,
                    json.dumps(sources, ensure_ascii=False),
                    json.dumps(stats, ensure_ascii=False),
                    now,
                    uid,
                    digest_date,
                ),
            )
        return self.get(uid, digest_date) or {}

    def save_failed(self, user_id: str | None, digest_date: str, error: str) -> None:
        """记录失败状态；即使生成前未成功建行也不能静默丢失失败信息。"""
        uid = normalize_user_id(user_id)
        now = _now()
        error_text = str(error or "failed")[:200]
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO ai_news_digests "
                "(user_id, digest_date, status, content, sources_json, stats_json, error, created_at, updated_at) "
                "VALUES (?, ?, 'failed', '', '[]', '{}', ?, ?, ?) "
                "ON CONFLICT(user_id, digest_date) DO UPDATE SET "
                "status='failed', error=excluded.error, updated_at=excluded.updated_at",
                (uid, digest_date, error_text, now, now),
            )

    def get(self, user_id: str | None, digest_date: str | None = None) -> dict[str, Any] | None:
        uid = normalize_user_id(user_id)
        with db_connection() as conn:
            if digest_date:
                row = conn.execute(
                    "SELECT * FROM ai_news_digests WHERE user_id=? AND digest_date=?",
                    (uid, digest_date),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM ai_news_digests WHERE user_id=? AND status='ready' "
                    "ORDER BY digest_date DESC, id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
        return self._decode(row) if row else None

    def list(self, user_id: str | None, limit: int = 20) -> list[dict[str, Any]]:
        uid = normalize_user_id(user_id)
        bounded = max(1, min(int(limit), 100))
        with db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_news_digests WHERE user_id=? AND status='ready' "
                "ORDER BY digest_date DESC, id DESC LIMIT ?",
                (uid, bounded),
            ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        value = dict(row)
        for key in ("sources_json", "stats_json"):
            try:
                value[key[:-5]] = json.loads(value.pop(key) or ("[]" if key == "sources_json" else "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                value[key[:-5]] = [] if key == "sources_json" else {}
        return value


repository = AINewsRepository()

__all__ = ["AINewsRepository", "repository"]
