#!/usr/bin/env bash
# 编译入口（macOS / Linux）—— 与 start.ps1 功能一一对应，仅转发到 build.sh
# 用法: ./start.sh [arupa|nomad|publish|all] [release|debug]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$ROOT/build.sh"
JOBS=8

# 单步执行：标题 + build.py 参数。失败即中止（set -e），退出码透传。
step() {
    local title="$1"
    shift
    printf '\n\n=======> %s\n' "$title"
    bash "$BUILD" "$@"
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
cmd="${1:-all}"
cmd="${cmd,,}"
case "$cmd" in
    arupa)
        build_arupa
        ;;
    nomad)
        variant="${2:-release}"
        variant="${variant,,}"
        case "$variant" in
            release|debug)
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
