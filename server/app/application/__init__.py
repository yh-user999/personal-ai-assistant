"""应用用例层；具体用例采用懒加载，避免组合端口被提前展开。"""


def __getattr__(name: str):
    if name == "ChatApplication":
        from .chat import ChatApplication

        return ChatApplication
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ChatApplication"]
