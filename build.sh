#!/usr/bin/env bash
# 编译 / 测试 / 打包入口（macOS / Linux）—— 转发到 scripts/build.py
# 用法见: bash build.sh -h
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] 未找到 python3" >&2
    exit 1
fi

exec "$PY" "$ROOT/scripts/build.py" "$@"
