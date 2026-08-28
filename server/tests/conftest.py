"""pytest 共享配置：统一 sys.path 与测试环境隔离。

- app.*（服务端代码）需要 server/ 在 path 上
- collector.* / desktop.* / common.*（跨端模块）需要仓库根在 path 上
- 环境变量在 app.config 首次导入前兜底：本地 .env 里的真实 API_TOKEN 会让
  未处理鉴权的老测试 401——测试环境默认关鉴权，鉴权用例自行 monkeypatch
"""
import os
import sys
from pathlib import Path

SERVER_ROOT = str(Path(__file__).resolve().parents[1])
REPO_ROOT = str(Path(__file__).resolve().parents[2])

for _p in (SERVER_ROOT, REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("API_TOKEN", "")

