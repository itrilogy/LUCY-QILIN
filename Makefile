# 栖林商店 · 常用工程命令
#
# 项目只依赖 Python 标准库；枢纽验签另需 cryptography（pip install -e '.[hub]'）。
# 本机默认用系统 python3；要指定解释器：make test PYTHON=/path/to/python3

PYTHON ?= python3
PYTEST ?= pytest

.PHONY: help test check lint serve init-admin backup clean docker-build docker-up docker-down

help: ## 列出可用命令
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

test: ## 跑全量测试
	PYTHONPATH=. $(PYTHON) -m $(PYTEST) tests -q

check: ## 编译、门户 JS 语法、部署脚本语法（不跑测试）
	$(PYTHON) -m compileall -q store
	$(PYTHON) scripts/check_portal_js.py
	@for f in deploy/docker/*.sh; do sh -n "$$f"; done

serve: ## 本机起服务（127.0.0.1:8787，数据在 var/）
	PYTHONPATH=. $(PYTHON) -m store.cli serve --db var/qilin.db --blobs var/blobs

init-admin: ## 空库时创建第一个管理员
	PYTHONPATH=. $(PYTHON) -m store.cli init-admin

backup: ## 备份 var/ 到 var/backups/<时间戳>
	PYTHONPATH=. $(PYTHON) -m store.cli backup "var/backups/$$(date +%Y%m%dT%H%M%S)" --db var/qilin.db --blobs var/blobs

clean: ## 清生成物（不动 var/ 与 .git）
	find . -name __pycache__ -not -path './.git/*' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info

docker-build: ## 构建运行镜像
	docker compose build

docker-up: ## 起容器（只有 127.0.0.1:8787）
	docker compose up -d

docker-down: ## 停容器（保留数据卷/目录）
	docker compose down
