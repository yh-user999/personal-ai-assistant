"""自建 OneBot v11 HTTP 网关。"""


def __getattr__(name: str):
    if name == "GatewaySettings":
        from .config import GatewaySettings

        return GatewaySettings
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["GatewaySettings", "create_app"]
