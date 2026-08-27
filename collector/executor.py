"""执行器（第 11 课）：轮询服务器指令队列 → 本地执行 → 回传结果。

安全分级：
- open：os.startfile（打开文件/文件夹/应用），不执行命令
- list_dir / read_file：仅限服务器白名单（EXECUTOR_ALLOWED_ROOTS）校验通过的指令
- 轮询间隔 5s；失败回传 failed（服务器会记录，不会重试同一指令）
"""
import asyncio
import ctypes
import json
import logging
import os
import re
import shutil
import time

import httpx

logger = logging.getLogger("collector.executor")

MAX_LIST = 300  # 单次最多列出的条目数（防病态大目录刷屏）
# 双路径操作：target 为 JSON 数组（服务器打包）
TWO_PATH_ACTIONS = ("copy", "backup", "move", "rename")
SENSITIVE_PATTERN = re.compile(
    r"(恢复码|密码|口令|密钥|私钥|token|secret|password|api[_\-]?key)", re.IGNORECASE
)


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
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
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
                shown = entries[:MAX_LIST]
                body = [f"共 {total} 项"]
                if hidden:
                    body[-1] += f"（已隐藏 {hidden} 个系统/隐藏项）"
                if not dirs and not files:
                    body.append("（空目录）")
                else:
                    if dirs:
                        body.append("")
                        body.append(f"📁 文件夹（{len(dirs)}）：")
                        body.append("")
                        body.append(" · ".join(f"{n}/" for n, d in shown if d))
                    if files:
                        body.append("")
                        body.append(f"📄 文件（{len(files)}）：")
                        body.append("")
                        body.append(" · ".join(n for n, d in shown if not d))
                if total > MAX_LIST:
                    body.append("")
                    body.append(f"… 其余 {total - MAX_LIST} 项（可进入子目录继续查看）")
                text = "\n".join(body)
                sensitive = [n for n, _ in entries if SENSITIVE_PATTERN.search(n)]
                if sensitive:
                    names = "、".join(sensitive[:3])
                    text += f"\n⚠️ 发现疑似敏感名称：{names} —— 建议改名或移入 KeePassXC 加密保险库"
                return True, text
            if action == "read_file":
                if not os.path.isfile(target):
                    return False, f"文件不存在：{target}"
                with open(target, encoding="utf-8", errors="replace") as f:
                    content = f.read(2000)
                return True, content
            # ── 第 13 课：文件手（双路径操作；脚本脚不支持远程执行——安全分级③）──
            if action in TWO_PATH_ACTIONS:
                try:
                    parts = json.loads(target)
                    src, dst = str(parts[0]), str(parts[1])
                except Exception:
                    return False, "指令格式错误（需要 JSON 双路径）"
                return self._file_op(action, src, dst)
            return False, f"未知指令类型：{action}"
        except Exception as e:
            return False, f"执行出错：{e}"

    @staticmethod
    def _resolve_dst(src: str, dst: str, backup: bool = False) -> str:
        if backup:
            sub = "backup-" + time.strftime("%Y%m%d-%H%M%S")
            base = dst if (dst.endswith(("/", "\\")) or os.path.isdir(dst)) else os.path.dirname(dst) or dst
            return os.path.join(base, sub)
        if os.path.isdir(dst) or dst.endswith(("/", "\\")):
            return os.path.join(dst, os.path.basename(src.rstrip("/\\")))
        return dst

    def _file_op(self, action: str, src: str, dst: str) -> tuple[bool, str]:
        """文件手：copy / backup / move / rename（与桌面执行器同格式）。"""
        if not os.path.exists(src):
            return False, f"源不存在：{src}"
        if action in ("copy", "backup"):
            dst = self._resolve_dst(src, dst, backup=(action == "backup"))
            if os.path.isdir(src):
                existed = os.path.exists(dst)
                shutil.copytree(src, dst, dirs_exist_ok=True)
                note = "（目标已存在，已合并内容）" if existed else ""
                verb = "已备份" if action == "backup" else "已复制"
                return True, f"{verb}文件夹：{src} → {dst}{note}"
            if action == "backup":
                os.makedirs(dst, exist_ok=True)
                dst = os.path.join(dst, os.path.basename(src))
            existed = os.path.isfile(dst)
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
            note = "（已覆盖同名文件）" if existed else ""
            verb = "已备份" if action == "backup" else "已复制"
            size = os.path.getsize(dst)
            return True, f"{verb}：{src} → {dst}{note}（{size} B）"
        # move / rename
        dst = self._resolve_dst(src, dst)
        if action == "rename" and not os.path.dirname(dst):
            dst = os.path.join(os.path.dirname(src) or ".", dst)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        try:
            shutil.move(src, dst)
        except (FileExistsError, shutil.Error) as e:
            return False, f"操作失败（目标已存在同名项）：{e}"
        verb = "已重命名" if action == "rename" else "已移动"
        return True, f"{verb}：{src} → {dst}"

    def stop(self) -> None:
        self._running = False
