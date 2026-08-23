"""服务器 API 客户端（httpx 同步，供 QThread 使用）。"""
import os

import httpx


class ApiClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
        self.token = os.environ.get("COLLECTOR_TOKEN", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def chat(self, message: str) -> str:
        r = httpx.post(
            f"{self.base_url}/api/chat",
            json={"message": message},
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["reply"]

    def stats_summary(self, days: int = 7) -> dict:
        r = httpx.get(f"{self.base_url}/api/stats/summary", params={"days": days}, timeout=15)
        r.raise_for_status()
        return r.json()
