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

from common import launcher
from common.file_ops import (
    copy_impl,
    exec_ext,
    is_blocked_open,
    list_dir_text,
    move_impl,
    path_allowed,
    read_file_text,
    rename_impl,
    search_files_impl,
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
        self._pending_results: list[dict] = []  # 回传未确认的执行结果（下轮重推）

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

        # 先重传上次未确认的执行结果（幂等：服务器 mark_result 可重复回传）
        if self._pending_results:
            await self._flush_results()

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
        # 回传失败不入库即丢——加入待确认队列下轮重推
        self._pending_results.append({"id": cmd["id"], "ok": ok, "result": result})
        await self._flush_results()

    async def _flush_results(self) -> None:
        """重传积压的执行结果；确认送达的从队列移除。"""
        remaining = []
        for item in self._pending_results:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/api/executor/result",
                    json=item,
                    headers=self._headers(),
                )
                if resp.status_code == 409:
                    # 服务端已接受/命令已过期/重复回传：对当前结果停止重试，
                    # 否则一次网络重试后的 409 会让内存队列永久增长。
                    logger.info("结果 #%s 已被服务端确认或拒绝重复回传", item["id"])
                elif resp.status_code != 200:
                    logger.warning(
                        "结果回传失败 #%s（下轮重推）: HTTP %s",
                        item["id"], resp.status_code,
                    )
                    remaining.append(item)
            except Exception as e:
                logger.warning("结果回传异常 #%s（下轮重推）: %s", item["id"], e)
                remaining.append(item)
        self._pending_results = remaining

    def _execute(self, action: str, target: str) -> tuple[bool, str]:
        """执行单一指令（同步，to_thread 包裹）。"""
        try:
            if action == "open":
                # 注册别名是显式授权，可指向白名单外路径；原始路径则必须白名单。
                # 注意顺序：只有"不像路径"的目标才允许走别名解析——否则用户给出
                # 明确路径（F:/我的微信资料）会被模糊匹配劫持成启动 exe，
                # 等于绕过白名单与扩展名黑名单（见 launcher.try_launch 的 strict 参数）。
                launched, ok, text = launcher.try_launch(target)
                if launched:
                    return ok, text
                if is_blocked_open(target):
                    return False, (
                        f"出于安全考虑，不允许打开脚本/安装包类型：{exec_ext(target) or target}"
                    )
                if not path_allowed(target):
                    return False, "🔒 打开目标超出白名单（EXECUTOR_ALLOWED_ROOTS），本地已拒绝"
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
            # ── 第 6.24 课：文件搜索（JSON [目录, 关键词]；目录空=全白名单）──
            if action == "search_files":
                try:
                    parts = json.loads(target)
                    dir_spec, keyword = str(parts[0]), str(parts[1])
                except Exception:
                    return False, "指令格式错误（需要 JSON [目录, 关键词]）"
                if dir_spec and not path_allowed(dir_spec):
                    return False, "🔒 搜索目录超出白名单（EXECUTOR_ALLOWED_ROOTS），本地已拒绝"
                return search_files_impl(dir_spec, keyword)
            return False, f"未知指令类型：{action}"
        except Exception as e:
            return False, f"执行出错：{e}"

    def stop(self) -> None:
        self._running = False

    async def aclose(self) -> None:
        """关闭长驻 HTTP 客户端（进程退出前调用）。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
