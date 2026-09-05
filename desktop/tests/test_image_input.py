from __future__ import annotations

import sys
from pathlib import Path

import pytest

DESKTOP_DIR = Path(__file__).resolve().parents[1]
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import api_client


class _Response:
    def __init__(self, payload=None):
        self.payload = payload or {"reply": "ok"}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_api_client_chat_json(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(api_client.httpx, "post", fake_post)
    client = api_client.ApiClient()
    assert client.chat("hello", timeout=12) == "ok"
    url, kwargs = calls[0]
    assert url.endswith("/api/chat")
    assert kwargs["json"]["message"] == "hello"
    assert "request_id" in kwargs["json"]
    assert kwargs["timeout"] == 12
    assert "files" not in kwargs


def test_api_client_chat_multipart_reads_image_only(monkeypatch, tmp_path):
    image = tmp_path / "shot.PNG"
    image.write_bytes(b"png-bytes")
    calls = []

    def fake_post(url, **kwargs):
        files = kwargs["files"]
        filename, stream, media_type = files["image"]
        calls.append((url, kwargs["data"], filename, stream.read(), media_type))
        return _Response()

    monkeypatch.setattr(api_client.httpx, "post", fake_post)
    client = api_client.ApiClient()
    assert client.chat("", image_path=image) == "ok"
    url, data, filename, content, media_type = calls[0]
    assert url.endswith("/api/chat/vision")
    assert data["message"] == ""
    assert "request_id" in data
    assert filename == "shot.PNG"
    assert content == b"png-bytes"
    assert media_type == "image/png"
    assert image.read_bytes() == b"png-bytes"


def test_choose_image_uses_file_dialog_path(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    import chat_panel

    image = tmp_path / "picked.png"
    image.write_bytes(b"image")
    selected = []
    panel = chat_panel.ChatPanel.__new__(chat_panel.ChatPanel)
    panel._set_attachment = lambda path: selected.append(path) or True
    monkeypatch.setattr(
        chat_panel.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(image), "图片 (*.png)"),
    )

    panel._choose_image()

    assert selected == [str(image)]


def test_clipboard_image_is_saved_and_cleared_as_temp_file(tmp_path):
    pytest.importorskip("PySide6")
    import chat_panel
    from PySide6.QtGui import QImage

    class Label:
        def __init__(self):
            self.text = ""

        def setText(self, value):
            self.text = value

    class Button:
        def setEnabled(self, value):
            self.enabled = value

    panel = chat_panel.ChatPanel.__new__(chat_panel.ChatPanel)
    panel._attachment_path = None
    panel._attachment_temp = False
    panel._attachment_label = Label()
    panel._clear_attachment_btn = Button()
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0x336699)

    panel._on_clipboard_image(image)
    path = panel._attachment_path

    assert path and Path(path).exists()
    assert panel._attachment_temp is True
    panel._clear_attachment()
    assert not Path(path).exists()


def test_chat_worker_passes_image_path():
    PySide6 = pytest.importorskip("PySide6")
    del PySide6
    from chat_workers import _ChatWorker

    class Client:
        def __init__(self):
            self.calls = []

        def chat(self, message, image_path=None):
            self.calls.append((message, image_path))
            return "reply"

    client = Client()
    worker = _ChatWorker(client, "caption", image_path="/tmp/photo.png")
    received = []
    worker.done.connect(lambda role, text: received.append((role, text)))
    worker.run()
    assert client.calls == [("caption", "/tmp/photo.png")]
    assert received == [("assistant", "reply")]


def test_image_only_send_skips_local_executor(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    import chat_panel

    image = tmp_path / "photo.png"
    image.write_bytes(b"image")
    created = []
    local_called = []

    class FakeInput:
        def text(self):
            return ""

        def clear(self):
            pass

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeChatWorker:
        def __init__(self, client, message, image_path=None):
            created.append((client, message, image_path))
            self.done = FakeSignal()

        def start(self):
            created.append("started")

    class ForbiddenLocalWorker:
        def __init__(self, message):
            local_called.append(message)
            raise AssertionError("图片-only 不应进入本地执行器")

    panel = chat_panel.ChatPanel.__new__(chat_panel.ChatPanel)
    panel.input = FakeInput()
    panel._worker = None
    panel._local_worker = None
    panel.ball = None
    panel.client = object()
    panel._pending_msg = ""
    panel._pending_image_path = None
    panel._pending_image_temp = False
    panel._take_attachment = lambda: (str(image), False)
    panel._append = lambda *args, **kwargs: None
    panel._show_typing = lambda: None
    monkeypatch.setattr(chat_panel, "_ChatWorker", FakeChatWorker)
    monkeypatch.setattr(chat_panel, "_LocalExecWorker", ForbiddenLocalWorker)

    panel._send()
    assert created == [(panel.client, "", str(image)), "started"]
    assert local_called == []
