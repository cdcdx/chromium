#!/usr/bin/env bash
# 源码修改备份入口（macOS / Linux）—— 转发到 scripts/backup.py
# 用法见: bash backup.sh -h
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] 未找到 Python 3.9+，请先安装" >&2
    exit 1
fi

exec "$PY" "$ROOT/scripts/backup.py" "$@"
