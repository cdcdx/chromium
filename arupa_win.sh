#!/usr/bin/env bash
#
# build_arupa_unix.sh -- Arupa 内核 Unix 编译脚本 (Chromium component build)
#
# 对应 Windows 侧 build_arupa_win.ps1；本脚本在 macOS / Linux 上把 arupa_win
# 编进 Chromium 树，产出 libarupa_kernel.{so,dylib}（及 Linux/mac 的 arupa_render
# 子进程薄壳）。
#
# 前提（脚本只检查，不负责安装）:
#   1. 完整 Chromium 源码树（默认 $REPO_ROOT/src，可用 -ChromiumSrc 指定）
#   2. depot_tools 在 PATH（提供 gn / autoninja）；工作区内 depot_tools/ 也会自动加
#   3. 本仓 arupa_win/ 放在 $REPO_ROOT/arupa_win
#
# 挂载约定: arupa_win 通过软链挂到 $CHROMIUM_SRC/chrome/browser/arupa_desktop
#   （BUILD.gn 全文用 //chrome/browser/arupa_desktop:* 引用，挂错位置 = 编错树）。
#   加 -NoLink 可跳过软链（编的是挂载点当前那份）。
#
# 输出目录（out/ 下）:
#   组件构建 (dynamic) : arupa-{os}-{arch}-{version}
#   静态构建 (static)  : arupa-{os}-{arch}-{version}-static
#   {os}=mac|linux  {arch}=目标 CPU（如 arm64/x64）  {version}=源码树 chrome/VERSION
#
# 用法:
#   # 默认: Release, 组件构建, 自动探测 Chromium 路径与宿主架构, 编内核 + render
#   ./build_arupa_unix.sh
#
#   # 显式 Chromium 路径
#   ./build_arupa_unix.sh -ChromiumSrc /path/to/chromium/src
#
#   # Debug 构建
#   ./build_arupa_unix.sh -BuildType Debug
#
#   # 干净重编
#   ./build_arupa_unix.sh -Clean
#
#   # 只生成 ninja（不编译）
#   ./build_arupa_unix.sh -GenOnly
#
#   # 指定目标
#   ./build_arupa_unix.sh -Target "arupa_kernel render"
#
#   # 静态链接（默认 dynamic / 组件构建；非组件构建需要 Linux TLS 补丁，脚本会自打）
#   ./build_arupa_unix.sh -LinkMode static
#
set -euo pipefail

# ========== 基础路径 ==========
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_REPO="$REPO_ROOT/arupa_win"
MODULE_REL_PATH="chrome/browser/arupa_desktop"
GN_ALL_MARKER="\"//${MODULE_REL_PATH}:arupa_kernel\","

# ========== 默认参数 ==========
CHROMIUM_SRC=""
BUILD_TYPE="Release"
TARGET_CPU=""
LINK_MODE="static"           # dynamic=组件构建, static=静态
CLEAN=0
GEN_ONLY=0
NO_LINK=0
TARGETS=""
LIST_TARGETS=0
JOBS=0

# ========== 参数解析 ==========
while [[ $# -gt 0 ]]; do
    case "$1" in
        -ChromiumSrc)  CHROMIUM_SRC="$2"; shift 2;;
        -BuildType)    BUILD_TYPE="$2"; shift 2;;
        -TargetCpu)    TARGET_CPU="$2"; shift 2;;
        -LinkMode)     LINK_MODE="$2"; shift 2;;
        -Target)       TARGETS="$2"; shift 2;;
        -Jobs)         JOBS="$2"; shift 2;;
        -Clean)        CLEAN=1; shift;;
        -GenOnly)      GEN_ONLY=1; shift;;
        -NoLink)       NO_LINK=1; shift;;
        -ListTargets)  LIST_TARGETS=1; shift;;
        -h|-help|--help) sed -n '3,60p' "$0"; exit 0;;
        *) echo "[ERROR] 未知参数: $1" >&2; exit 1;;
    esac
done

# ========== 助手 ==========
c_yellow=$'\033[1;33m'; c_cyan=$'\033[1;36m'; c_green=$'\033[1;32m'
c_red=$'\033[1;31m'; c_reset=$'\033[0m'
step()  { echo; echo "${c_cyan}==> $*${c_reset}"; }
ok()    { echo "    ${c_green}[ OK  ] $*${c_reset}"; }
warn()  { echo "    ${c_yellow}[ WARN ] $*${c_reset}" >&2; }
err()   { echo "    ${c_red}[ERROR] $*${c_reset}" >&2; exit 1; }

# 宿主 OS / 架构
HOST_OS="$(uname -s)"
case "$HOST_OS" in
    Darwin) HOST_OS_NAME="mac";;
    Linux)  HOST_OS_NAME="linux";;
    *) err "不支持的宿主系统: $HOST_OS（仅 macOS / Linux）";;
esac
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64|aarch64) HOST_ARCH_NORM="arm64";;
    x86_64|amd64)  HOST_ARCH_NORM="x64";;
    i386|i686)     HOST_ARCH_NORM="x86";;
    *) HOST_ARCH_NORM="$HOST_ARCH";;
esac
[[ -z "$TARGET_CPU" ]] && TARGET_CPU="$HOST_ARCH_NORM"

# arupa 版本：取自 Chromium 源码树 chrome/VERSION（MAJOR.MINOR.BUILD.PATCH），
# 避免手抄漂移；缺失则回退到 .env 的 chromium_ver，再无则空串（空串时输出目录不含版本段）。
arupa_version() {
    local f="$CHROMIUM_SRC/chrome/VERSION"
    if [[ -f "$f" ]]; then
        local maj min bld pat
        while IFS='=' read -r k v; do
            case "$k" in
                MAJOR)  maj="$v";;
                MINOR)  min="$v";;
                BUILD)  bld="$v";;
                PATCH)  pat="$v";;
            esac
        done < "$f"
        if [[ -n "${maj:-}${min:-}${bld:-}${pat:-}" ]]; then
            echo "$maj.$min.$bld.$pat"
            return
        fi
    fi
    if [[ -f "$REPO_ROOT/.env" ]]; then
        local v
        v="$(grep -E '^[[:space:]]*chromium_ver[[:space:]]*=' "$REPO_ROOT/.env" 2>/dev/null \
             | head -n1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//')"
        [[ -n "$v" ]] && { echo "$v"; return; }
    fi
    echo ""
}

# ========== -ListTargets（提前退出） ==========
if [[ "$LIST_TARGETS" -eq 1 ]]; then
    build_gn="$KERNEL_REPO/BUILD.gn"
    [[ -f "$build_gn" ]] || err "BUILD.gn 未找到: $build_gn"
    echo "BUILD.gn 中定义的目标:"
    echo
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*(shared_library|executable|source_set|mojom|group)\(\[[:space:]]*\"([^\"]+)\"[[:space:]]]*\) ]]; then
            kind="${BASH_REMATCH[1]}"
            name="${BASH_REMATCH[2]}"
            printf "  %-16s %s\n" "[$kind]" "$name"
        fi
    done < "$build_gn"
    echo
    echo "用 -Target 'name' 指定构建目标。"
    exit 0
fi

# ========== 1. 定位 Chromium 源码树 ==========
step "定位 Chromium 源码树"
echo "    Repo root     : $REPO_ROOT"

if [[ -z "$CHROMIUM_SRC" ]]; then
    if [[ -n "${CHROMIUM_SRC:-}" ]]; then
        CHROMIUM_SRC="$CHROMIUM_SRC"
    elif [[ -d "$REPO_ROOT/src" ]]; then
        CHROMIUM_SRC="$REPO_ROOT/src"
    else
        err "无法定位 Chromium 源码树。用 -ChromiumSrc 指定，或把源码树放在 $REPO_ROOT/src"
    fi
fi
# 规范化
CHROMIUM_SRC="$(cd "$CHROMIUM_SRC" 2>/dev/null && pwd)" || err "Chromium 源码树路径无效: $CHROMIUM_SRC"

required_dirs=(build content base mojo DEPS)
missing=()
for d in "${required_dirs[@]}"; do
    [[ -e "$CHROMIUM_SRC/$d" ]] || missing+=("$d")
done
[[ ${#missing[@]} -eq 0 ]] || err "Chromium 树不完整，缺: ${missing[*]}"

ok "Chromium src  : $CHROMIUM_SRC"

# ========== 2. 检查内核仓 ==========
step "检查内核仓 (arupa_win)"
[[ -d "$KERNEL_REPO" ]] || err "内核仓不存在: $KERNEL_REPO"
[[ -f "$KERNEL_REPO/BUILD.gn" ]] || err "内核仓缺 BUILD.gn: $KERNEL_REPO/BUILD.gn"
ok "Kernel repo   : $KERNEL_REPO"

# ========== 3. 挂载模块到 Chromium 树 ==========
step "挂载模块到 Chromium 树 ($MODULE_REL_PATH)"
MODULE_ABS="$CHROMIUM_SRC/$MODULE_REL_PATH"

if [[ "$NO_LINK" -eq 1 ]]; then
    warn "-NoLink: 跳过软链检查（编的是挂载点当前那份）"
else
    want="$(cd "$KERNEL_REPO" && pwd)"
    if [[ -L "$MODULE_ABS" ]]; then
        real="$(readlink -f "$MODULE_ABS")"
        if [[ "$real" == "$want" ]]; then
            ok "已挂载       : $MODULE_ABS -> $real"
        else
            # 已指向别的目录：删掉旧映射，重建指向当前内核仓（避免『改 A 树编 B 树』还卡住）
            warn "挂载点已指向别的目录，删除旧映射并重建: $real -> $want"
            rm -f "$MODULE_ABS"
            ln -s "$KERNEL_REPO" "$MODULE_ABS"
            ok "已重建软链   : $MODULE_ABS -> $KERNEL_REPO"
        fi
    elif [[ -e "$MODULE_ABS" ]]; then
        err "挂载点已存在且不是软链: $MODULE_ABS（手动处理或 -NoLink）"
    else
        parent="$(dirname "$MODULE_ABS")"
        mkdir -p "$parent"
        ln -s "$KERNEL_REPO" "$MODULE_ABS"
        ok "已软链       : $MODULE_ABS -> $KERNEL_REPO"
    fi
fi

# 检查 api/ 与 mojom/（BUILD.gn 引用的必需子目录）
api_dir="$MODULE_ABS/api"
mojom_dir="$MODULE_ABS/mojom"
[[ -d "$api_dir" ]]   || warn "缺 api/   （扩展 API schema，*.json，构建必需）"
[[ -d "$mojom_dir" ]] || warn "缺 mojom/ （mojom 接口定义，*.mojom）"

# ========== 4. 检查工具链 (depot_tools) ==========
step "检查 GN/Ninja 工具链 (depot_tools)"
# 工作区内 depot_tools 自动加进 PATH
if [[ -d "$REPO_ROOT/depot_tools" ]]; then
    export PATH="$REPO_ROOT/depot_tools:$PATH"
fi
command -v gn >/dev/null 2>&1         || err "gn 未找到，确保 depot_tools 在 PATH"
command -v autoninja >/dev/null 2>&1  || err "autoninja 未找到，确保 depot_tools 在 PATH"
export DEPOT_TOOLS_UPDATE=0
export DEPOT_TOOLS_METRICS=0
ok "gn           : $(command -v gn)"
ok "autoninja    : $(command -v autoninja)"
echo "    gn version: $(gn --version 2>&1 | head -n1)"

# ========== 5. Linux 专属补丁 ==========
if [[ "$HOST_OS_NAME" == "linux" ]]; then
    step "Linux 专属补丁: v8 / blink TLS 模型"
    v8_gn="$CHROMIUM_SRC/v8/BUILD.gn"
    [[ -f "$v8_gn" ]] || err "缺 $v8_gn（源码树不完整？）"
    if grep -q 'v8_tls_used_in_library = false' "$v8_gn"; then
        ok "src/v8/BUILD.gn 已有 v8_tls_used_in_library 开关"
    else
        # 1) 声明开关（在 v8_monolithic_for_shared_library = false 之后）
        # 2) 在 v8_monolithic && v8_monolithic_for_shared_library 的 defines 块之后加分支
        perl -0777 -i -pe '
            s/(\n  v8_monolithic_for_shared_library = false\n)/
$1\n  # arupa: 非 component 构建把 v8 静态链进 dlopen 的 .so 时，v8 默认\n  # tls_model="local-exec" 在共享对象里非法 —— mold\/ld 直接报\n  # "recompile with -fPIC"（误导）。本开关让 v8 退回 local-dynamic。\n  v8_tls_used_in_library = false\n/ unless $seen_decl;
            s/(\n  if \(v8_monolithic && v8_monolithic_for_shared_library\) \{\n    defines \+= \[ "V8_TLS_USED_IN_LIBRARY" \]\n  \}\n)/
$1\n  if (v8_tls_used_in_library) {\n    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n  }\n/;
        ' "$v8_gn"
        # 防止 perl 把已插入的标记再次匹配：补一次幂等判定
        if grep -q 'v8_tls_used_in_library = false' "$v8_gn"; then
            ok "已给 src/v8/BUILD.gn 加 v8_tls_used_in_library 开关"
        else
            err "给 src/v8/BUILD.gn 打补丁失败（v8 结构变了？）"
        fi
    fi
fi

# ========== 6. 把内核目标挂进根 BUILD.gn 的 gn_all ==========
step "把内核目标挂进根 BUILD.gn 的 gn_all"
ROOT_GN="$CHROMIUM_SRC/BUILD.gn"
[[ -f "$ROOT_GN" ]] || err "缺 $ROOT_GN（源码树不完整？）"
if grep -qF "$GN_ALL_MARKER" "$ROOT_GN"; then
    ok "根 BUILD.gn 已挂本项目内核目标: $GN_ALL_MARKER"
else
    # 1) 摘掉别的内核挂载点条目（arupa_bigbang 等也可能产出同名 arupa_kernel）
    perl -0777 -i -pe "s{^[ \\t]*\"//chrome/browser/(?!arupa_desktop:)[^\"]*:arupa_kernel\",[ \\t]*\\n}{}mg" "$ROOT_GN"
    # 2) 插入到 group(\"gn_all\") 的 deps 块起始处
    if grep -qF "$GN_ALL_MARKER" "$ROOT_GN"; then
        ok "根 BUILD.gn: 已摘掉别的内核挂载点条目，只留本项目"
    else
        if perl -0777 -i -pe "s{(\n  if \(is_cronet_build\) \{\n.*?\n  \} else \{\n    deps = \[\n)}{\$1      $GN_ALL_MARKER\n}m" "$ROOT_GN"; then
            if grep -qF "$GN_ALL_MARKER" "$ROOT_GN"; then
                ok "已向根 BUILD.gn 的 gn_all 追加: $GN_ALL_MARKER"
            else
                err "根 BUILD.gn 里找不到 gn_all 的 deps 块（Chromium 结构变了？）—— 需手动加: $GN_ALL_MARKER"
            fi
        else
            err "向根 BUILD.gn 追加内核目标失败"
        fi
    fi
fi

# ========== 6b. 确保 :render 目标对当前宿主系统可解析 ==========
# 有的内核仓（如 arupa_win）只在 if (is_win) 里定义真正的 render 可执行文件，
# 而 macOS/Linux 上一批 testonly 目标用 data_deps = [":render"] 引用它 —— 缺了就
# gn gen "Unresolved dependencies"。与 Android 的 if (is_android) group("render") {}
# 同思路：当前平台没有 render_main 源文件时，补一个空 group 顶上，使图可解析、
# arupa_kernel 能编（代价：该平台不产出 arupa_render 子进程可执行文件）。
if [[ "$HOST_OS_NAME" == "mac" || "$HOST_OS_NAME" == "linux" ]]; then
    step "确保 :render 目标对 $HOST_OS_NAME 可解析"
    build_gn="$KERNEL_REPO/BUILD.gn"
    [[ -f "$build_gn" ]] || err "内核 BUILD.gn 不存在: $build_gn"
    case "$HOST_OS_NAME" in
        mac)   render_src="$KERNEL_REPO/content/render_main_mac.cc";;
        linux) render_src="$KERNEL_REPO/content/render_main_linux.cc";;
    esac
    if [[ -f "$render_src" ]]; then
        ok "存在 $render_src，BUILD.gn 应已定义该平台的 render 可执行文件"
    else
        marker="# arupa: fallback render group for $HOST_OS_NAME (no render_main source)"
        if grep -qF "$marker" "$build_gn"; then
            ok "已注入 $HOST_OS_NAME 的 render 兜底 group"
        else
            printf '\n%s\nif (is_%s) {\n' "$marker" "$HOST_OS_NAME" >> "$build_gn"
            printf '  # arupa: 本仓库只在 is_win 定义真正的 render 可执行文件；当前平台缺\n' >> "$build_gn"
            printf '  # render_main 源文件，testonly 目标的 data_deps=[":render"] 仍需它可解析，\n' >> "$build_gn"
            printf '  # 故补一个空 group 顶上（不产出 arupa_render 子进程可执行文件）。\n' >> "$build_gn"
            printf '  group("render") {}\n}\n' >> "$build_gn"
            ok "已向 BUILD.gn 追加 $HOST_OS_NAME 的 render 兜底 group"
        fi
    fi
fi

# ========== 7. 计算 GN args ==========
step "构建配置"
# 输出目录：arupa-{os}-{arch}-{version}（dynamic）/ ...-{version}-static（static）
ARUPA_VER="$(arupa_version)"
OUT_NAME="arupa-${HOST_OS_NAME}-${TARGET_CPU}"
[[ -n "$ARUPA_VER" ]] && OUT_NAME="$OUT_NAME-$ARUPA_VER"
[[ "$LINK_MODE" == "static" ]] && OUT_NAME="$OUT_NAME-static"
# 注意：输出目录放在仓库根 out/ 下（$REPO_ROOT/out/...），而不是 Chromium 源码树内的 src/out/。
OUT_DIR="$REPO_ROOT/out/$OUT_NAME"
echo "    Output dir    : $OUT_DIR"
echo "    Build type    : $BUILD_TYPE"
echo "    Target CPU    : $TARGET_CPU"
echo "    Link mode     : $LINK_MODE"
echo "    Arupa version : ${ARUPA_VER:-（未探测到，输出目录不含版本段）}"

is_debug="false"; [[ "$BUILD_TYPE" == "Debug" ]] && is_debug="true"
is_component="true"; [[ "$LINK_MODE" == "static" ]] && is_component="false"
dcheck="false";    [[ "$LINK_MODE" == "static" ]] && dcheck="false"  # 静态也关 dcheck，保规模

args_list=(
    "use_siso = false"
    "is_debug = $is_debug"
    "symbol_level = 0"
    "is_component_build = $is_component"
    "dcheck_always_on = $dcheck"
    "proprietary_codecs = true"
    "ffmpeg_branding = \"Chrome\""
    "target_cpu = \"$TARGET_CPU\""
)
if [[ "$HOST_OS_NAME" == "linux" && "$LINK_MODE" == "static" ]]; then
    # 非组件构建：v8/blink 静态链进 .so，需退回 local-dynamic TLS 模型
    args_list+=(
        "blink_heap_inside_shared_library = true"
        "v8_tls_used_in_library = true"
    )
fi

GN_ARGS="$(IFS=' '; echo "${args_list[*]}")"
echo "    GN args      : $GN_ARGS"

# ========== 8. 解析默认目标 ==========
if [[ -z "$TARGETS" ]]; then
    TARGETS="arupa_kernel"
    # Linux / macOS 需要 render 子进程薄壳（用完整 ninja 标签，裸 render 解析不到）
    if [[ "$HOST_OS_NAME" == "linux" || "$HOST_OS_NAME" == "mac" ]]; then
        TARGETS="$TARGETS chrome/browser/arupa_desktop:render"
    fi
fi

# ========== 9. Clean（可选） ==========
if [[ "$CLEAN" -eq 1 && -d "$OUT_DIR" ]]; then
    step "清理旧产物 (-Clean)"
    rm -rf "$OUT_DIR"
    ok "已删除 $OUT_DIR"
fi

# ========== 10. gn gen ==========
step "运行 gn gen"
mkdir -p "$OUT_DIR"
( cd "$CHROMIUM_SRC" && gn gen "$OUT_DIR" --args="$GN_ARGS" ) || err "gn gen 失败"
ok "gn gen 完成"

if [[ "$GEN_ONLY" -eq 1 ]]; then
    step "-GenOnly 已设，跳过编译"
    exit 0
fi

# ========== 11. autoninja 编译 ==========
step "运行 autoninja"
read -ra target_arr <<< "$TARGETS"
# 抬高栈上限：v8 巨型常量表达式会偶发 clang 爆栈（见 common.build_cmd 注释）。
# macOS 上硬限制常低于 unlimited/64MB，ulimit 抬不动属正常 —— 失败则忽略、不打印、
# 不终止（set -e 下需用 || true 兜底），沿用系统默认栈上限继续编。
ulimit -s unlimited 2>/dev/null || ulimit -s 65536 2>/dev/null || true
for t in "${target_arr[@]}"; do
    [[ -z "$t" ]] && continue
    # ninja 里 render 目标名是带目录标签的 chrome/browser/arupa_desktop:render，
    # 裸 "render" 解析不到（group 不像 shared_library 有短名别名），统一映射。
    [[ "$t" == "render" ]] && t="chrome/browser/arupa_desktop:render"
    echo "    构建        : $t"
    ninja_args=(-C "$OUT_DIR" "$t")
    [[ "$JOBS" -gt 0 ]] && ninja_args+=(-j "$JOBS")
    ( cd "$CHROMIUM_SRC" && autoninja "${ninja_args[@]}" ) || err "autoninja 构建 '$t' 失败"
done

# ========== 12. 定位产物 ==========
step "定位产物"
if [[ "$HOST_OS_NAME" == "linux" ]]; then
    art="$OUT_DIR/libarupa_kernel.so"; render="$OUT_DIR/arupa_render"
elif [[ "$HOST_OS_NAME" == "mac" ]]; then
    art="$OUT_DIR/libarupa_kernel.dylib"; render="$OUT_DIR/arupa_render"
else
    art="$OUT_DIR/arupa_kernel.dll"
fi

if [[ -f "$art" ]]; then
    sz=$(du -h "$art" | cut -f1)
    ok "$(basename "$art")  $sz  -> $art"
else
    warn "$(basename "$art") 未在 $OUT_DIR 找到"
fi
if [[ -n "${render:-}" ]]; then
    if [[ -f "$render" ]]; then
        ok "$(basename "$render")  -> $render"
    else
        warn "$(basename "$render") 未在 $OUT_DIR 找到"
    fi
fi

echo
echo "========================================"
echo "  构建完成!"
echo "========================================"
