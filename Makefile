# 个人智能助手 · 常用命令

.PHONY: help server test lint clean

SERVER_PYTHON := $(CURDIR)/server/.venv/bin/python
SERVER_RUFF := $(CURDIR)/server/.venv/bin/ruff

help:
	@echo "server   - 使用 server/.venv 启动服务端"
	@echo "test     - 使用 server/.venv 运行服务端测试"
	@echo "lint     - 使用 server/.venv 内 ruff 检查服务端代码"
	@echo "clean    - 清理缓存文件"

server:
	$(SERVER_PYTHON) server/run.py

test:
	$(SERVER_PYTHON) -m pytest server/tests/ -v

lint:
	$(SERVER_RUFF) check --select E4,E7,E9,F --ignore E402,E731 server/app/ server/tests/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
