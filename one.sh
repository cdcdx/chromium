#!/usr/bin/env bash
# 统一入口（macOS / Linux）：fetch / build / backup
#
# 统一命令与选项层：共享选项可以写在命令之前，由本脚本解析、校验并按子命令
# 能力转发；子命令其余参数（位置参数、专属选项）原样透传，退出码原样返回。
# 等价关系：
#   ./one.sh fetch  [...]  =  bash fetch.sh  [...]
#   ./one.sh build  [...]  =  bash build.sh  [...]
#   ./one.sh backup [...]  =  bash backup.sh [...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
用法: ./one.sh [全局选项] <命令> [命令选项/参数...]

命令:
  fetch                  拉取 / 更新 depot_tools、Chromium、DEPS 及业务仓库
  build                  编译 / 打包内核与浏览器
  backup                 将 src 的净修改备份为可恢复的二进制 Git 补丁
  help [命令]            显示帮助；带命令时等价于 [命令] -h

全局选项（可写在命令之前；按子命令能力校验）:
  --os <系统>            目标系统（fetch / build / backup）
  --arch <架构>          目标架构（fetch / build）
  --ver <X.Y.Z.W>        Chromium 版本（fetch / build / backup）
  --jobs, -j <N>         并发任务数（fetch / build）
  --dry-run              仅打印计划，不写文件、不联网（三个命令）

backup 专属（同样可写在 backup 之前）:
  --base <基线>          --ver 的别名，本地 tag / branch / commit
  --num <N>              备份序号，正整数；不指定则自动递增
  --src <目录>           源码 Git 仓库根目录（默认工作区 src）
  --patches, --output <目录>  备份根目录（默认工作区 patches）
  --tracked-only         不包含未跟踪文件（仍包含已暂存的新文件）

子命令其余参数原样透传，详见: ./one.sh <命令> -h
EOF
}

# 退出码：2 = 参数 / 用法错误（与 Python argparse 一致）；1 = 环境错误（缺 Python）
fail() {
    printf '[ERROR] %s\n\n' "$*" >&2
    usage >&2
    exit 2
}

# 与旧入口完全相同的探测顺序：优先 python3，其次 python，要求 3.9+
find_python() {
    local c
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1 &&
           "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
            printf '%s' "$c"
            return 0
        fi
    done
    return 1
}

# run_py <脚本名> <缺 Python 时的提示后缀> [参数...]
run_py() {
    local script="$1"
    local hint="$2"
    shift 2

    local py
    if ! py="$(find_python)"; then
        printf '[ERROR] 未找到 Python 3.9+%s\n' "$hint" >&2
        exit 1
    fi
    exec "$py" "$ROOT/scripts/$script.py" "$@"
}

lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

hint_of() {
    case "$1" in
        fetch) printf '%s' ' —— depot_tools 也依赖它，请先安装' ;;
        build) printf '%s' '' ;;
        backup) printf '%s' '，请先安装' ;;
    esac
}

# 子命令能接受的全局选项（新增共享选项时只改这里）
allowed_of() {
    case "$1" in
        fetch|build) printf '%s' ' os arch ver jobs dry-run ' ;;
        backup) printf '%s' ' os ver num src patches tracked-only dry-run ' ;;
    esac
}

# 共享选项按 "--name value" 归一化，最后统一追加到子命令参数尾部
# （argparse 与位置无关，追加不改变语义）
g_names=()
g_tokens=()

add_global_value() {
    local name="$1"
    local val="$2"

    if [ "$name" = 'jobs' ] || [ "$name" = 'num' ]; then
        case "$val" in
            ''|*[!0-9]*) fail "--$name 必须是正整数（收到: $val）" ;;
        esac
        [ "$val" -gt 0 ] || fail "--$name 必须是正整数（收到: $val）"
    fi

    g_names+=("$name")
    g_tokens+=("--$name" "$val")
}

add_global_flag() {
    g_names+=("$1")
    g_tokens+=("--$1")
}

# 把用户输入的选项归一化为 canonical 名称（用于能力校验与 token 拼接）
canon_name() {
    case "$1" in
        -j|--jobs) printf '%s' 'jobs' ;;
        --base) printf '%s' 'ver' ;;
        --output) printf '%s' 'patches' ;;
        --*) printf '%s' "${1#--}" ;;
    esac
}

cmd=''
while [ $# -gt 0 ]; do
    case "$1" in
        --os|--arch|--ver|--base|--jobs|-j|--num|--src|--patches|--output)
            opt="$1"
            shift
            if [ $# -eq 0 ] || [ "${1#-}" != "$1" ]; then
                fail "$opt 需要一个值"
            fi
            val="$1"
            shift
            add_global_value "$(canon_name "$opt")" "$val"
            ;;
        --os=*|--arch=*|--ver=*|--base=*|--jobs=*|--num=*|--src=*|--patches=*|--output=*)
            opt="${1%%=*}"
            val="${1#*=}"
            shift
            [ -n "$val" ] || fail "$opt 需要一个值"
            add_global_value "$(canon_name "$opt")" "$val"
            ;;
        --dry-run|--tracked-only)
            add_global_flag "$(canon_name "$1")"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            [ $# -gt 0 ] || fail '缺少命令'
            cmd="$1"
            shift
            break
            ;;
        -?*)
            fail "未知全局选项: $1"
            ;;
        *)
            cmd="$1"
            shift
            break
            ;;
    esac
done

[ -n "$cmd" ] || { usage >&2; exit 2; }
cmd="$(lower "$cmd")"

if [ "$cmd" = 'help' ]; then
    target="${1:-}"
    if [ -z "$target" ]; then
        usage
        exit 0
    fi
    target="$(lower "$target")"
    case "$target" in
        fetch|build|backup)
            run_py "$target" "$(hint_of "$target")" -h
            ;;
        *)
            fail "未知命令: $target"
            ;;
    esac
fi

case "$cmd" in
    fetch|build|backup) ;;
    *) fail "未知命令: $cmd" ;;
esac

allowed="$(allowed_of "$cmd")"
for n in ${g_names[@]+"${g_names[@]}"}; do
    case "$allowed" in
        *" $n "*) ;;
        *) fail "--$n 不适用于 $cmd" ;;
    esac
done

run_py "$cmd" "$(hint_of "$cmd")" "$@" ${g_tokens[@]+"${g_tokens[@]}"}
