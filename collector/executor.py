"""执行器（第 11 课）：轮询服务器指令队列 → 本地执行 → 回传结果。

安全分级：
- open：os.startfile（打开文件/文件夹/应用），不执行命令
- list_dir / read_file：仅限服务器白名单（EXECUTOR_ALLOWED_ROOTS）校验通过的指令
- 轮询间隔 5s；失败回传 failed（服务器会记录，不会重试同一指令）
"""
import asyncio
import ctypes
import logging
import os
import re

import httpx

logger = logging.getLogger("collector.executor")

MAX_LIST = 300  # 单次最多列出的条目数（防病态大目录刷屏）


def _natural_key(name: str) -> list:
    """自然排序键：大小写不敏感 + 数字按数值比（f2 < f10）。"""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def _is_hidden_system(path: str) -> bool:
    """Windows 隐藏/系统属性判断（Explorer 默认不显示）；非 Windows 恒为 False。"""
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        return attrs != 0xFFFFFFFF and bool(attrs & (0x2 | 0x4))  # HIDDEN|SYSTEM
    except Exception:
        return False


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
                if not os.path.isdir(target):
                    return False, f"目录不存在：{target}"
                hidden = 0
                entries: list[tuple[str, bool]] = []  # (name, is_dir)
                for name in os.listdir(target):
                    full = os.path.join(target, name)
                    if _is_hidden_system(full):
                        hidden += 1
                        continue
                    entries.append((name, os.path.isdir(full)))
                # 自然排序：大小写不敏感、数字按数值、文件夹在前（Explorer 习惯）
                entries.sort(key=lambda it: _natural_key(it[0]))
                dirs = [n for n, d in entries if d]
                files = [n for n, d in entries if not d]
                total = len(entries)
                lines = [f"- 📁 {n}/" for n, d in entries[:MAX_LIST] if d]
                lines += [f"- 📄 {n}" for n, d in entries[:MAX_LIST] if not d]
                if not lines:
                    lines = ["（空目录）"]
                if total > MAX_LIST:
                    lines.append(f"… 其余 {total - MAX_LIST} 项")
                header = f"共 {total} 项（📁 {len(dirs)} 文件夹 / 📄 {len(files)} 文件"
                if hidden:
                    header += f"，已隐藏 {hidden} 个系统/隐藏项"
                header += "）："
                return True, header + "\n" + "\n".join(lines)
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
