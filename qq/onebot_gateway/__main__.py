"""手工启动入口：``python -m qq.onebot_gateway``。"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import uvicorn

from .app import app
from .config import settings


def main() -> None:
    settings.validate()
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
