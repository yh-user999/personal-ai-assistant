"""服务器 API 客户端（httpx 同步，供 QThread 使用）。

所有请求 trust_env=False：服务器是 Tailscale 内网地址，直连即可；
不继承系统代理设置（代理连不上内网会把聊天长请求搞成 502）。
"""
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from ssh_tunnel import SshTunnelConfig, SshTunnelError, get_shared_tunnel_manager


_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class NovelWorkbenchError(RuntimeError):
    """小说工作台 URL 或隧道准备失败（错误文本已脱敏）。"""


class ApiClient:
    def __init__(self, tunnel_manager=None) -> None:
        self.base_url = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
        self.token = os.environ.get("API_TOKEN", "")
        self._tunnel_manager = tunnel_manager or get_shared_tunnel_manager()

    def novel_tunnel_config(self) -> SshTunnelConfig | None:
        """读取小说工作台隧道配置；未配置目标时返回 None。"""
        if not os.environ.get("NOVEL_TUNNEL_TARGET", "").strip():
            return None
        return SshTunnelConfig.from_env()

    @staticmethod
    def _validate_novel_web_url(url: str) -> str:
        candidate = url.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise NovelWorkbenchError("NOVEL_WEB_URL 必须是完整的 http(s) 网页地址")
        if parsed.username or parsed.password:
            raise NovelWorkbenchError("NOVEL_WEB_URL 不支持在地址中嵌入账号信息")
        return candidate

    def novel_workbench_url(self) -> str:
        """解析小说工作台网页地址，不启动隧道。"""
        configured_url = os.environ.get("NOVEL_WEB_URL", "").strip()
        if configured_url:
            return self._validate_novel_web_url(configured_url)
        config = self.novel_tunnel_config()
        if config is not None:
            return f"http://127.0.0.1:{config.local_port}/novel/"
        return f"{self.base_url}/novel/"

    def prepare_novel_workbench(self) -> str:
        """准备小说工作台访问地址；需要时先确保共享 SSH 隧道已就绪。"""
        configured_url = os.environ.get("NOVEL_WEB_URL", "").strip()
        if configured_url:
            return self._validate_novel_web_url(configured_url)
        config = self.novel_tunnel_config()
        if config is None:
            return self.novel_workbench_url()
        url = f"http://127.0.0.1:{config.local_port}/novel/"
        try:
            self._tunnel_manager.ensure_ready(config)
        except SshTunnelError as exc:
            raise NovelWorkbenchError(str(exc)) from exc
        except Exception as exc:
            raise NovelWorkbenchError("小说工作台隧道准备失败，请检查桌面端配置") from exc
        return url

    def close_novel_tunnel(self) -> None:
        """关闭共享管理器创建的隧道；手动已有隧道不会被关闭。"""
        self._tunnel_manager.close()

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

    def chat(self, message: str, image_path: str | os.PathLike[str] | None = None, timeout: float | None = None) -> str:
        """发送文本或图片消息；图片文件只读打开，调用方拥有其生命周期。"""
        request_id = uuid.uuid4().hex
        request_timeout = (90 if image_path is not None else 60) if timeout is None else timeout
        if image_path is None:
            r = httpx.post(
                f"{self.base_url}/api/chat",
                json={"message": message, "request_id": request_id},
                headers=self._headers(),
                timeout=request_timeout,
                trust_env=False,
            )
        else:
            path = Path(image_path)
            media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
            if media_type is None:
                raise ValueError("仅支持 JPEG、PNG 或 WebP 图片")
            with path.open("rb") as image_file:
                r = httpx.post(
                    f"{self.base_url}/api/chat/vision",
                    data={"message": message, "request_id": request_id},
                    files={"image": (path.name, image_file, media_type)},
                    headers=self._headers(),
                    timeout=request_timeout,
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
        """到期提醒（第 8 课后桌面不再轮询：QQ 推送是唯一通道；保留供调试）。"""
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
