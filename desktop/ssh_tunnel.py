"""Windows OpenSSH 隧道管理。

桌面端小说工作台只在点击入口时建立隧道：
- 使用参数列表调用系统 OpenSSH，不经过 shell；
- 复用已经监听本机端口的转发；
- 只记录本进程启动的 ssh 子进程，并在退出时关闭它；
- 所有公开错误都不回显目标地址、私钥路径或 ssh 原始 stderr。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class SshTunnelError(RuntimeError):
    """用户可见的、已脱敏的 SSH 隧道错误。"""


@dataclass(frozen=True)
class SshTunnelConfig:
    """建立一个本地端口转发所需的配置。"""

    target: str
    local_port: int = 18000
    remote_host: str = "127.0.0.1"
    remote_port: int = 8000
    identity_file: str | None = None

    @classmethod
    def from_env(cls) -> "SshTunnelConfig":
        target = os.environ.get("NOVEL_TUNNEL_TARGET", "").strip()
        local_port = _read_port("NOVEL_TUNNEL_LOCAL_PORT", 18000)
        remote_port = _read_port("NOVEL_TUNNEL_REMOTE_PORT", 8000)
        remote_host = os.environ.get("NOVEL_TUNNEL_REMOTE_HOST", "127.0.0.1").strip()
        if not remote_host:
            raise SshTunnelError("NOVEL_TUNNEL_REMOTE_HOST 不能为空")
        identity_file = os.environ.get("NOVEL_TUNNEL_IDENTITY_FILE", "").strip() or None
        return cls(
            target=target,
            local_port=local_port,
            remote_host=remote_host,
            remote_port=remote_port,
            identity_file=identity_file,
        )


def _read_port(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise SshTunnelError(f"{name} 必须是 1-65535 的端口") from exc
    if not 1 <= port <= 65535:
        raise SshTunnelError(f"{name} 必须是 1-65535 的端口")
    return port


def resolve_ssh_executable() -> str:
    """查找 Windows 自带 OpenSSH，测试和打包环境可通过 PATH 提供 ssh。"""
    for name in ("ssh.exe", "ssh"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    # 让 Popen 给出统一的“未找到 OpenSSH”错误，而不是在这里暴露平台路径。
    return "ssh.exe" if os.name == "nt" else "ssh"


def build_ssh_command(config: SshTunnelConfig, ssh_executable: str | None = None) -> list[str]:
    """构造不经过 shell 的 ssh 参数列表。"""
    target = config.target.strip()
    if not target:
        raise SshTunnelError("未配置 NOVEL_TUNNEL_TARGET，无法建立 SSH 隧道")
    if target.startswith("-") or any(char.isspace() for char in target):
        raise SshTunnelError("NOVEL_TUNNEL_TARGET 配置无效，请使用 SSH config alias 或 user@host")
    if not 1 <= int(config.local_port) <= 65535:
        raise SshTunnelError("本机隧道端口必须是 1-65535")
    if not 1 <= int(config.remote_port) <= 65535:
        raise SshTunnelError("远端隧道端口必须是 1-65535")
    remote_host = config.remote_host.strip()
    if not remote_host or any(char.isspace() for char in remote_host):
        raise SshTunnelError("NOVEL_TUNNEL_REMOTE_HOST 配置无效")

    command = [
        ssh_executable or resolve_ssh_executable(),
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if config.identity_file:
        command.extend(["-i", str(Path(config.identity_file).expanduser())])
    command.extend([
        "-L",
        f"{int(config.local_port)}:{remote_host}:{int(config.remote_port)}",
        target,
    ])
    return command


def is_local_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """检查本机端口是否已有可连接的转发。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class SshTunnelManager:
    """线程安全地复用或管理一个小说工作台 SSH 隧道。"""

    def __init__(
        self,
        *,
        ssh_executable: str | None = None,
        startup_timeout: float = 10.0,
        probe_interval: float = 0.1,
        socket_timeout: float = 0.25,
        port_probe: Callable[[int, float], bool] | None = None,
        popen_factory: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        self.ssh_executable = ssh_executable
        self.startup_timeout = max(0.0, float(startup_timeout))
        self.probe_interval = max(0.0, float(probe_interval))
        self.socket_timeout = max(0.01, float(socket_timeout))
        self._port_probe = port_probe or (
            lambda port, timeout: is_local_port_open(port, timeout=timeout)
        )
        self._popen_factory = popen_factory or subprocess.Popen
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._config: SshTunnelConfig | None = None

    @property
    def process(self) -> subprocess.Popen | None:
        """返回本进程管理的 ssh 进程，仅供测试/诊断使用。"""
        with self._lock:
            return self._process

    def ensure_ready(self, config: SshTunnelConfig) -> None:
        """确保 config 对应的本机转发可用；已有监听端口直接复用。"""
        with self._lock:
            if self._process is not None:
                same_config = self._config == config
                if same_config and self._process.poll() is None and self._port_is_open(config.local_port):
                    return
                self._stop_owned_process_locked()

            # 端口已由用户手动启动的 ssh -L 占用时，不触碰它。
            if self._port_is_open(config.local_port):
                self._config = config
                return

            command = build_ssh_command(config, self.ssh_executable)
            try:
                process = self._popen_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError as exc:
                raise SshTunnelError("未找到 Windows OpenSSH，请确认 ssh.exe 已安装并在 PATH 中") from exc
            except OSError as exc:
                raise SshTunnelError("SSH 隧道启动失败，请检查 OpenSSH、SSH config 和密钥配置") from exc

            self._process = process
            self._config = config
            if self._wait_for_port(config.local_port):
                return

            exit_code = process.poll()
            self._stop_owned_process_locked()
            if exit_code is not None:
                raise SshTunnelError(f"SSH 隧道启动失败（退出码 {exit_code}），请检查 SSH config 和密钥配置")
            raise SshTunnelError("SSH 隧道未能在本机端口就绪，请检查 SSH config、密钥和远端服务")

    def close(self) -> None:
        """只关闭本进程创建的隧道，不影响用户手动启动的转发。"""
        with self._lock:
            self._stop_owned_process_locked()
            self._config = None

    def _port_is_open(self, port: int) -> bool:
        try:
            return bool(self._port_probe(int(port), self.socket_timeout))
        except Exception:
            return False

    def _wait_for_port(self, port: int) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        while True:
            if self._port_is_open(port):
                return True
            if self._process is not None and self._process.poll() is not None:
                return False
            if time.monotonic() >= deadline:
                return False
            if self.probe_interval:
                time.sleep(self.probe_interval)

    def _stop_owned_process_locked(self) -> None:
        process = self._process
        self._process = None
        self._config = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            # 退出阶段不再把 ssh 清理异常升级成 UI 错误。
            pass


_SHARED_MANAGER = SshTunnelManager()


def get_shared_tunnel_manager() -> SshTunnelManager:
    """返回桌面进程内共享的隧道管理器。"""
    return _SHARED_MANAGER
