"""服务器 API 客户端（httpx 同步，供 QThread 使用）。"""
import os

import httpx


class ApiClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
        self.token = os.environ.get("API_TOKEN", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def health(self) -> bool:
        """健康检查（短超时，供断线检测）。"""
        try:
            r = httpx.get(f"{self.base_url}/api/health", timeout=4)
            return r.status_code == 200
        except Exception:
            return False

    def executor_results(self, since_id: int = 0) -> list:
        """id > since_id 的已执行指令结果（面板轮询显示）。"""
        r = httpx.get(
            f"{self.base_url}/api/executor/results",
            params={"since_id": since_id},
            headers=self._headers(),
            timeout=8,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def greeting(self) -> str:
        """个性化问候（打开面板时刷新）。"""
        r = httpx.get(f"{self.base_url}/api/greeting", headers=self._headers(), timeout=8)
        r.raise_for_status()
        return r.json().get("greeting", "")

    def chat(self, message: str) -> str:
        r = httpx.post(
            f"{self.base_url}/api/chat",
            json={"message": message},
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["reply"]

    def recent_messages(self, limit: int = 30) -> list:
        """最近消息（打开面板时加载历史）。"""
        r = httpx.get(
            f"{self.base_url}/api/messages",
            params={"limit": limit},
            headers=self._headers(),
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("messages", [])

    def stats_summary(self, days: int = 7) -> dict:
        r = httpx.get(
            f"{self.base_url}/api/stats/summary",
            params={"days": days},
            headers=self._headers(),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def latest_report(self) -> dict | None:
        """最新周报（无则返回 None）。"""
        r = httpx.get(f"{self.base_url}/api/reports", headers=self._headers(), timeout=15)
        r.raise_for_status()
        reports = r.json().get("reports", [])
        if not reports:
            return None
        week = reports[0]["week"]
        rr = httpx.get(f"{self.base_url}/api/reports/{week}", headers=self._headers(), timeout=15)
        rr.raise_for_status()
        return rr.json()

    def latest_daily(self) -> dict | None:
        """最新每日小结（无则返回 None）。"""
        r = httpx.get(f"{self.base_url}/api/daily/latest", headers=self._headers(), timeout=15)
        r.raise_for_status()
        d = r.json()
        return d if d.get("exists") else None
