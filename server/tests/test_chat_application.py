"""ChatApplication 门面与组合根测试。"""
from types import SimpleNamespace

from app.application.chat import ChatApplication
from app.chat.context import ChatRequest, ChatResponse


class _LLM:
    async def chat(self, messages, **kwargs):
        return "ok"

    def chat_stream(self, messages, **kwargs):
        async def _stream():
            yield "ok"

        return _stream()


class _Memory:
    def __init__(self):
        self.context_calls = []

    async def write_message(self, *args, **kwargs):
        return None

    async def search(self, *args, **kwargs):
        return []

    def get_recent_history(self, *args, **kwargs):
        return []


def _application(monkeypatch):
    memory = _Memory()
    factory_calls = []

    def services_factory():
        factory_calls.append(True)
        return SimpleNamespace()

    app = ChatApplication(
        settings=SimpleNamespace(),
        llm=_LLM(),
        memory=memory,
        knowledge=SimpleNamespace(),
        services_factory=services_factory,
        bg_tasks=set(),
        logger=SimpleNamespace(),
    )
    return app, factory_calls


def test_build_runtime_uses_explicit_dependencies():
    app, factory_calls = _application(None)
    runtime = app.build_runtime()

    assert runtime.settings.__class__ is SimpleNamespace
    assert runtime.llm is app.llm
    assert runtime.memory is app.memory
    assert runtime.knowledge is app.knowledge
    assert factory_calls == [True]


def test_run_delegates_context_and_pipeline(monkeypatch):
    app, _factory_calls = _application(monkeypatch)
    sentinel = ChatResponse(reply="应用层", memories_used=0)
    seen = {}

    def fake_context(request_model, request, memory):
        seen["context"] = (request_model, request, memory)
        return "context"

    async def fake_run(context, runtime):
        seen["run"] = (context, runtime)
        return sentinel

    monkeypatch.setattr("app.application.chat.build_context", fake_context)
    monkeypatch.setattr("app.application.chat.run_chat", fake_run)
    request = object()
    model = ChatRequest(message="你好")

    result = __import__("asyncio").run(app.run(model, request))

    assert result is sentinel
    assert seen["context"] == (model, request, app.memory)
    assert seen["run"][0] == "context"


def test_composition_does_not_import_http_api():
    import app.chat.composition as composition

    assert composition.get_chat_application().settings is not None
    assert "app.api.chat" not in composition.__dict__
