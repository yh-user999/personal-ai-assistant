"""执行器（第 11 课）：轮询服务器指令队列 → 本地执行 → 回传结果。

安全分级：
- open：os.startfile（打开文件/文件夹/应用），不执行命令
- list_dir / read_file：仅限服务器白名单（EXECUTOR_ALLOWED_ROOTS）校验通过的指令
- 轮询间隔 5s；失败回传 failed（服务器会记录，不会重试同一指令）
"""
import asyncio
import logging
import os

import httpx

logger = logging.getLogger("collector.executor")


class Executor:
    def __init__(self, server_url: str, token: str = "", interval: float = 5.0):
        self.base_url = server_url.rstrip("/")
        self.token = token
        self.interval = interval
        self._running = True

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
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.base_url}/api/executor/pending", headers=self._headers()
            )
            if r.status_code != 200:
                return
            cmd = r.json().get("command")
            if not cmd:
                return
            logger.info("执行指令 #%s: %s %s", cmd["id"], cmd["action"], cmd["target"])
            ok, result = await asyncio.to_thread(self._execute, cmd["action"], cmd["target"])
            await client.post(
                f"{self.base_url}/api/executor/result",
                json={"id": cmd["id"], "ok": ok, "result": result},
                headers=self._headers(),
            )

    def _execute(self, action: str, target: str) -> tuple[bool, str]:
        """执行单一指令（同步，to_thread 包裹）。"""
        try:
            if action == "open":
                os.startfile(target)
                return True, f"已打开 {target}"
            if action == "list_dir":
                entries = sorted(os.listdir(target))
                dirs = [e for e in entries if os.path.isdir(os.path.join(target, e))]
                files = [e for e in entries if not os.path.isdir(os.path.join(target, e))]
                shown_dirs, shown_files = dirs[:20], files[:20]
                lines = [f"- 📁 {d}/" for d in shown_dirs]
                lines += [f"- 📄 {f}" for f in shown_files]
                shown = len(shown_dirs) + len(shown_files)
                text = "\n".join(lines)
                if len(entries) > shown:
                    text += f"\n… 其余 {len(entries) - shown} 项"
                return (
                    True,
                    f"{target} 共 {len(entries)} 项"
                    f"（📁 {len(dirs)} 文件夹 / 📄 {len(files)} 文件）：\n{text}",
                )
            if action == "read_file":
                if not os.path.isfile(target):
                    return False, f"文件不存在：{target}"
                with open(target, encoding="utf-8", errors="replace") as f:
                    content = f.read(2000)
                return True, content
            return False, f"未知指令类型：{action}"
        except Exception as e:
            return False, f"执行出错：{e}"

    def stop(self) -> None:
        self._running = False
