# 个人智能助手 · 常用命令

.PHONY: help server test lint clean

help:
	@echo "server   - 启动服务端 (cd server && python run.py)"
	@echo "test     - 运行服务端测试"
	@echo "lint     - ruff 检查服务端代码"
	@echo "clean    - 清理缓存文件"

server:
	cd server && python run.py

test:
	cd server && python -m pytest tests/ -v

lint:
	cd server && ruff check app/ tests/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
