# 个人智能助手 · 常用命令
# venv 布局双平台兼容：Windows 是 Scripts/，Linux/macOS 是 bin/。

.PHONY: help server test lint clean

ifeq ($(OS),Windows_NT)
	VENV_BIN := server/.venv/Scripts
else
	VENV_BIN := server/.venv/bin
endif

SERVER_PYTHON := $(CURDIR)/$(VENV_BIN)/python
SERVER_RUFF := $(CURDIR)/$(VENV_BIN)/ruff

help:
	@echo "server   - 使用 server/.venv 启动服务端"
	@echo "test     - 使用 server/.venv 运行服务端测试"
	@echo "lint     - 使用 server/.venv 内 ruff 检查全部 Python 代码"
	@echo "clean    - 清理缓存文件"

server:
	$(SERVER_PYTHON) server/run.py

test:
	$(SERVER_PYTHON) -m pytest server/tests/ -v

# 规则统一维护在根 pyproject.toml，此处不再重复命令行参数。
lint:
	$(SERVER_RUFF) check .

clean:
	rm -rf $(shell find . -type d -name __pycache__ 2>/dev/null) || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
