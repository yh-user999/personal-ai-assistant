"""服务器 API 客户端（httpx 同步，供 QThread 使用）。

所有请求 trust_env=False：服务器是 Tailscale 内网地址，直连即可；
不继承系统代理设置（代理连不上内网会把聊天长请求搞成 502）。
"""
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
            r = httpx.get(f"{self.base_url}/api/health", timeout=4, trust_env=False)
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
            trust_env=False,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def greeting(self) -> str:
        """个性化问候（打开面板时刷新）。"""
        r = httpx.get(
            f"{self.base_url}/api/greeting", headers=self._headers(), timeout=8, trust_env=False
        )
        r.raise_for_status()
        return r.json().get("greeting", "")

    def chat(self, message: str) -> str:
        r = httpx.post(
            f"{self.base_url}/api/chat",
            json={"message": message},
            headers=self._headers(),
            timeout=60,
            trust_env=False,
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
            trust_env=False,
        )
        r.raise_for_status()
        return r.json().get("messages", [])

    def stats_summary(self, days: int = 7) -> dict:
        r = httpx.get(
            f"{self.base_url}/api/stats/summary",
            params={"days": days},
            headers=self._headers(),
            timeout=15,
            trust_env=False,
        )
        r.raise_for_status()
        return r.json()

    def latest_report(self) -> dict | None:
        """最新周报（无则返回 None）。"""
        r = httpx.get(f"{self.base_url}/api/reports", headers=self._headers(), timeout=15, trust_env=False)
        r.raise_for_status()
        reports = r.json().get("reports", [])
        if not reports:
            return None
        week = reports[0]["week"]
        rr = httpx.get(f"{self.base_url}/api/reports/{week}", headers=self._headers(), timeout=15, trust_env=False)
        rr.raise_for_status()
        return rr.json()

    def latest_daily(self) -> dict | None:
        """最新每日小结（无则返回 None）。"""
        r = httpx.get(
            f"{self.base_url}/api/daily/latest", headers=self._headers(), timeout=15, trust_env=False
        )
        r.raise_for_status()
        d = r.json()
        return d if d.get("exists") else None

    def due_reminders(self) -> list:
        """到期提醒（服务器取出即标记已推送，桌面端只负责弹窗）。"""
        try:
            r = httpx.get(
                f"{self.base_url}/api/reminders/due",
                headers=self._headers(),
                timeout=8,
                trust_env=False,
            )
            r.raise_for_status()
            return r.json().get("reminders", [])
        except Exception:
            return []

    def search_messages(self, query: str) -> dict | None:
        """消息全文检索（第 6.26 课）；失败返回 None。"""
        try:
            r = httpx.get(
                f"{self.base_url}/api/messages/search",
                params={"q": query},
                headers=self._headers(),
                timeout=10,
                trust_env=False,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def mood_state(self) -> dict | None:
        """情绪状态（第 6.28 课 C2：体贴模式轮询）；失败返回 None。"""
        try:
            r = httpx.get(
                f"{self.base_url}/api/mood/state",
                headers=self._headers(),
                timeout=8,
                trust_env=False,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None
