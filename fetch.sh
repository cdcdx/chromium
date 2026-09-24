#!/usr/bin/env bash
# 源码 / 依赖拉取更新入口（macOS / Linux）—— 转发到 scripts/fetch.py
# 用法见: bash fetch.sh -h
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] 未找到 python3 —— depot_tools 也依赖它，请先安装" >&2
    exit 1
fi

exec "$PY" "$ROOT/scripts/fetch.py" "$@"
