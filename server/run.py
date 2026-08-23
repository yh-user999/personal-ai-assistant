"""个人智能助手 · 服务端启动入口

用法: python run.py  （读取 ../.env，监听 $HOST:$PORT，默认 0.0.0.0:8000）
"""
import os
import sys
from pathlib import Path

# 允许直接以 `python run.py` 运行（app 包在 server/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 加载 .env（server/../.env）
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
