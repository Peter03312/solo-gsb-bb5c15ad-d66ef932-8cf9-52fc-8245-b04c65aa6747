#!/bin/sh
set -e

# 确保数据目录可写并完成幂等初始化
mkdir -p "$(dirname "$SEAL_DB_PATH")"
python /app/init_db.py

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
