"""执行器（第 11 课）：轮询服务器指令队列 → 本地执行 → 回传结果。

安全分级：
- open：os.startfile（打开文件/文件夹/应用）；脚本/安装包扩展名黑名单拒绝执行
- list_dir / read_file / 文件手：本地白名单（EXECUTOR_ALLOWED_ROOTS）复核后才执行，
  不信任服务端——token 持有者可绕过聊天解析直调入队 API
- 轮询间隔 5s；失败回传 failed（服务器会记录，指令认领后不会重试）

文件操作的公共实现在 common/file_ops.py（与桌面执行器共用）。
"""
import asyncio
import json
import logging
import os

import httpx
from common.file_ops import (
    OPEN_BLOCKED_EXTS,
    copy_impl,
    list_dir_text,
    move_impl,
    path_allowed,
    read_file_text,
    rename_impl,
)

logger = logging.getLogger("collector.executor")

# 双路径操作：target 为 JSON 数组（服务器打包）
TWO_PATH_ACTIONS = ("copy", "backup", "move", "rename")


class Executor:
    def __init__(self, server_url: str, token: str = "", interval: float = 5.0):
        self.base_url = server_url.rstrip("/")
        self.token = token
        self.interval = interval
        self._running = True
        self._client: httpx.AsyncClient | None = None  # 长驻客户端，避免每 5s 重建

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def run(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.warning("执行器轮询失败: %s", e)
            await asyncio.sleep(self.interval)

    async def _poll_once(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15, trust_env=False)
        r = await self._client.get(
            f"{self.base_url}/api/executor/pending", headers=self._headers()
        )
        if r.status_code != 200:
            return
        cmd = r.json().get("command")
        if not cmd:
            return
        logger.info("执行指令 #%s: %s %s", cmd["id"], cmd["action"], cmd["target"])
        ok, result = await asyncio.to_thread(self._execute, cmd["action"], cmd["target"])
        resp = await self._client.post(
            f"{self.base_url}/api/executor/result",
            json={"id": cmd["id"], "ok": ok, "result": result},
            headers=self._headers(),
        )
        if resp.status_code != 200:
            # 指令已认领不会重跑，但结果丢了必须留痕排障
            logger.warning("结果回传失败 #%s: HTTP %s", cmd["id"], resp.status_code)

    def _execute(self, action: str, target: str) -> tuple[bool, str]:
        """执行单一指令（同步，to_thread 包裹）。"""
        try:
            if action == "open":
                ext = os.path.splitext(target)[1].lower()
                if ext in OPEN_BLOCKED_EXTS:
                    return False, f"出于安全考虑，不允许打开脚本/安装包类型：{ext}"
                os.startfile(target)
                return True, f"已打开 {target}"
            if action == "list_dir":
                if not path_allowed(target):
                    return False, "🔒 目标路径超出白名单（EXECUTOR_ALLOWED_ROOTS），本地已拒绝"
                return list_dir_text(target)
            if action == "read_file":
                if not path_allowed(target):
                    return False, "🔒 目标路径超出白名单（EXECUTOR_ALLOWED_ROOTS），本地已拒绝"
                return read_file_text(target)
            # ── 第 13 课：文件手（双路径操作；脚本脚不支持远程执行——安全分级③）──
            if action in TWO_PATH_ACTIONS:
                try:
                    parts = json.loads(target)
                    src, dst = str(parts[0]), str(parts[1])
                except Exception:
                    return False, "指令格式错误（需要 JSON 双路径）"
                if not (path_allowed(src) and path_allowed(dst)):
                    return False, "🔒 目标路径超出白名单（EXECUTOR_ALLOWED_ROOTS），本地已拒绝"
                if action == "copy":
                    return copy_impl(src, dst)
                if action == "backup":
                    return copy_impl(src, dst, backup=True)
                if action == "move":
                    return move_impl(src, dst)
                return rename_impl(src, dst)
            return False, f"未知指令类型：{action}"
        except Exception as e:
            return False, f"执行出错：{e}"

    def stop(self) -> None:
        self._running = False

    async def aclose(self) -> None:
        """关闭长驻 HTTP 客户端（进程退出前调用）。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
