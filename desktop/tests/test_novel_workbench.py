from __future__ import annotations

import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import api_client
import ssh_tunnel


class _FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_ssh_command_uses_argument_list_and_no_shell():
    calls = []
    process = _FakeProcess()
    probes = iter((False, True))

    def probe(port, timeout):
        return next(probes)

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    manager = ssh_tunnel.SshTunnelManager(
        ssh_executable="ssh.exe",
        startup_timeout=0.1,
        probe_interval=0,
        port_probe=probe,
        popen_factory=popen,
    )
    config = ssh_tunnel.SshTunnelConfig(
        target="desktop-alias",
        local_port=18000,
        remote_host="127.0.0.1",
        remote_port=8000,
        identity_file="C:/Users/example/.ssh/id_ed25519",
    )

    manager.ensure_ready(config)

    command, kwargs = calls[0]
    assert isinstance(command, list)
    assert command[0] == "ssh.exe"
    assert command[-3:] == [
        "-L",
        "18000:127.0.0.1:8000",
        "desktop-alias",
    ]
    assert "ExitOnForwardFailure=yes" in command
    assert "ServerAliveInterval=30" in command
    assert "ServerAliveCountMax=3" in command
    assert "BatchMode=yes" in command
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is not None
    assert process is manager.process

    manager.close()
    assert process.terminated is True
    assert manager.process is None


def test_existing_local_forward_is_reused_without_starting_process():
    called = []

    def popen(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("已有本机转发时不应启动 ssh")

    manager = ssh_tunnel.SshTunnelManager(
        port_probe=lambda port, timeout: True,
        popen_factory=popen,
    )
    manager.ensure_ready(ssh_tunnel.SshTunnelConfig(target="desktop-alias"))

    assert called == []
    assert manager.process is None
    manager.close()


def test_api_client_generates_local_novel_url_and_prepares_tunnel(monkeypatch):
    monkeypatch.delenv("NOVEL_WEB_URL", raising=False)
    monkeypatch.setenv("NOVEL_TUNNEL_TARGET", "desktop-alias")
    monkeypatch.setenv("NOVEL_TUNNEL_LOCAL_PORT", "18001")
    monkeypatch.setenv("NOVEL_TUNNEL_REMOTE_HOST", "127.0.0.1")
    monkeypatch.setenv("NOVEL_TUNNEL_REMOTE_PORT", "8000")

    class Manager:
        def __init__(self):
            self.configs = []

        def ensure_ready(self, config):
            self.configs.append(config)

        def close(self):
            pass

    manager = Manager()
    client = api_client.ApiClient(tunnel_manager=manager)

    assert client.prepare_novel_workbench() == "http://127.0.0.1:18001/novel/"
    assert len(manager.configs) == 1
    assert manager.configs[0].target == "desktop-alias"
    assert manager.configs[0].local_port == 18001


def test_explicit_novel_url_overrides_tunnel_and_never_gets_api_token(monkeypatch):
    secret = "token-that-must-not-appear"
    monkeypatch.setenv("API_TOKEN", secret)
    monkeypatch.setenv("NOVEL_WEB_URL", "https://workbench.example/novel/")
    monkeypatch.setenv("NOVEL_TUNNEL_TARGET", "desktop-alias")

    class Manager:
        def __init__(self):
            self.called = False

        def ensure_ready(self, config):
            self.called = True

        def close(self):
            pass

    manager = Manager()
    client = api_client.ApiClient(tunnel_manager=manager)
    url = client.prepare_novel_workbench()

    assert url == "https://workbench.example/novel/"
    assert secret not in url
    assert manager.called is False


def test_fallback_url_uses_server_url_without_api_token(monkeypatch):
    secret = "another-token-that-must-not-appear"
    monkeypatch.setenv("API_TOKEN", secret)
    monkeypatch.setenv("SERVER_URL", "http://server.example:8000")
    monkeypatch.delenv("NOVEL_WEB_URL", raising=False)
    monkeypatch.delenv("NOVEL_TUNNEL_TARGET", raising=False)

    url = api_client.ApiClient().prepare_novel_workbench()

    assert url == "http://server.example:8000/novel/"
    assert secret not in url


def test_missing_target_and_start_failure_are_sanitized():
    with pytest.raises(ssh_tunnel.SshTunnelError, match="未配置") as missing:
        ssh_tunnel.build_ssh_command(ssh_tunnel.SshTunnelConfig(target=""))
    assert "NOVEL_TUNNEL_TARGET" in str(missing.value)

    def failing_popen(*args, **kwargs):
        raise OSError("private-host-and-key-material")

    manager = ssh_tunnel.SshTunnelManager(
        startup_timeout=0,
        port_probe=lambda port, timeout: False,
        popen_factory=failing_popen,
    )
    with pytest.raises(ssh_tunnel.SshTunnelError) as failed:
        manager.ensure_ready(ssh_tunnel.SshTunnelConfig(target="desktop-alias"))
    assert "private-host-and-key-material" not in str(failed.value)
    assert "SSH 隧道启动失败" in str(failed.value)


def test_novel_workbench_worker_returns_url_or_sanitized_error():
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    del app
    from chat_workers import _NovelWorkbenchWorker

    class Client:
        def prepare_novel_workbench(self):
            return "http://127.0.0.1:18000/novel/"

    received = []
    worker = _NovelWorkbenchWorker(Client())
    worker.done.connect(lambda url, error: received.append((url, error)))
    worker.run()

    assert received == [("http://127.0.0.1:18000/novel/", "")]
