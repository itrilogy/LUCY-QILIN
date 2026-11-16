#!/usr/bin/env sh
# 本地验收：与 CI 的必需步骤一致，另加 compileall。
set -eu
cd "$(dirname "$0")/.."
PYTHONPATH=. python3 -m pytest tests -q
python3 -m compileall -q store
python3 scripts/check_portal_js.py
echo "acceptance: ok"
