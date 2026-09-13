#!/usr/bin/env bash
# 启动本地服务：http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
  :
elif [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi

echo "using python: $PYTHON"
exec "$PYTHON" -m uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload
