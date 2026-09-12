#!/usr/bin/env python3
"""群聊主动插话 JSONL 离线评估入口。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat.social_replay import main


if __name__ == "__main__":
    raise SystemExit(main())
