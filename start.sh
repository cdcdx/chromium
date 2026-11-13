#!/usr/bin/env bash
# 编译入口（macOS / Linux）—— 与 start.ps1 功能一一对应，仅转发到 build.sh
# 用法: ./start.sh [arupa|nomad|publish|all] [release|debug]
set -euo pipefail

# export NomadApiEnvironment=test

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONE="$ROOT/one.sh"
JOBS=8
LOCK_DIR="$ROOT/out/.build.lock"

# 构建互斥：两次构建并发会互相删掉 out/.publish-* 暂存目录、抢占 obj 产物
# （Avalonia AVLN9999 "file in use"），因此同一时间只允许一个构建。
# macOS 自带 bash 3.2 没有 flock，用 mkdir 的原子性实现，持有者出错时按 PID 判定残留。
acquire_build_lock() {
    mkdir -p "$ROOT/out"
    local retries=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        local holder
        holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
            printf '[ERROR] 已有构建在运行（PID %s），并发构建会互相破坏产物，已中止\n' "$holder" >&2
            exit 1
        fi
        rm -rf "$LOCK_DIR"
        retries=$((retries + 1))
        if [ "$retries" -gt 2 ]; then
            printf '[ERROR] 获取构建锁失败: %s\n' "$LOCK_DIR" >&2
            exit 1
        fi
    done
    printf '%s' "$$" > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
}

# 单步执行：标题 + build.py 参数。失败即中止（set -e），退出码透传。
step() {
    local title="$1"
    shift
    printf '\n\n=======> %s\n' "$title"
    bash "$ONE" build "$@"
}

build_arupa() {
    step 'arupa gen build' arupa_desktop gen build --jobs "$JOBS"
    step 'arupa package' arupa_desktop package --zip
}

publish_arupa() {
    step 'arupa publish' arupa_desktop publish
}

build_nomad() {
    local variant="$1"
    step "nomad build ($variant)" nomad_desktop build --variant "$variant"
    step "nomad package ($variant)" nomad_desktop package --variant "$variant"
}

usage() {
    cat <<'EOF'
用法: ./start.sh [命令]

  arupa                  编译 + 打包 arupa 内核
  nomad [release|debug]  编译 + 打包 nomad（默认 release）
  publish                推送 arupa 交付包
  all                    编译 arupa + nomad（默认）
  help                   显示本帮助
EOF
}

# 命令 / 配置均按小写解析，与 start.ps1 的大小写不敏感行为保持一致
# 注意：使用 tr 而非 ${var,,}，macOS 自带 bash 3.2 不支持后者的扩展语法
lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

cmd="$(lower "${1:-all}")"
case "$cmd" in
    arupa)
        acquire_build_lock
        build_arupa
        ;;
    nomad)
        variant="$(lower "${2:-release}")"
        case "$variant" in
            release|debug)
                acquire_build_lock
                build_nomad "$variant"
                ;;
            *)
                printf '[ERROR] 未知构建配置: %s（可选 release / debug）\n' "$variant" >&2
                exit 1
                ;;
        esac
        ;;
    publish)
        publish_arupa
        ;;
    all)
        acquire_build_lock
        build_arupa
        build_nomad release
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        printf '[ERROR] 未知命令: %s\n\n' "$cmd" >&2
        usage >&2
        exit 1
        ;;
esac
