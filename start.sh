#!/usr/bin/env bash
# 编译入口（macOS / Linux）—— 与 start.ps1 功能一一对应，仅转发到 build.sh
# 用法: ./start.sh [arupa|nomad|publish|all] [release|debug] [test|prod]
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
    local variant="$1" env_name="$2"
    step "nomad build ($variant, $env_name)" nomad_desktop build --variant "$variant" --env "$env_name"
    step "nomad package ($variant, $env_name)" nomad_desktop package --variant "$variant" --env "$env_name"
}

# 与 build.py 的 choices 保持一致；非法值立刻报错，避免构建到一半才失败
check_variant() {
    case "$1" in
        release|debug) ;;
        *)
            printf '[ERROR] 未知构建配置: %s（可选 release / debug）\n' "$1" >&2
            exit 1
            ;;
    esac
}

check_env() {
    case "$1" in
        test|prod) ;;
        *)
            printf '[ERROR] 未知后端环境: %s（可选 test / prod）\n' "$1" >&2
            exit 1
            ;;
    esac
}

usage() {
    cat <<'EOF'
用法: ./start.sh [命令] [构建配置] [后端环境]

  arupa                            编译 + 打包 arupa 内核
  nomad [release|debug] [test|prod] 编译 + 打包 nomad（默认 release + test）
  publish                          推送 arupa 交付包
  all [release|debug] [test|prod]   编译 arupa + nomad（默认 release + test）
  help                             显示本帮助

后端环境 prod 会以 --env prod 传递，生成生产包（产物目录带 -prod 后缀）。
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
        env_name="$(lower "${3:-test}")"
        check_variant "$variant"
        check_env "$env_name"
        acquire_build_lock
        build_nomad "$variant" "$env_name"
        ;;
    publish)
        publish_arupa
        ;;
    all)
        variant="$(lower "${2:-release}")"
        env_name="$(lower "${3:-test}")"
        check_variant "$variant"
        check_env "$env_name"
        acquire_build_lock
        build_arupa
        build_nomad "$variant" "$env_name"
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
