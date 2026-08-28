"""项目进度事实刷新（薄封装 → app.services.progress_sync）。

用法：cd server && .venv/bin/python scripts/seed_progress_facts.py
幂等：先清理课程/项目类事实，再插入最新进度快照。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.progress_sync import refresh_progress_facts  # noqa: E402

if __name__ == "__main__":
    n = refresh_progress_facts()
    print(f"进度事实已刷新：{n} 条（旧课程事实已清理）")