#!/usr/bin/env bash
# ==== 用法开始
# 把 src/out/arupa-{os}-{arch}-{ver}-static 里的内核件整理成交付包：
#     dist/arupa-{os}-{ver}-static-{n}/kernel/{arch}/…
#
# 用法:
#   scripts/package_arupa.sh [选项]
#
# 输出（默认都带 -static，因为 Android 只有静态构建）：
#   dist/arupa-android-154.0.8037.21-static-1/
#     kernel/arm64/{arupa-kernel.aar, arupa_kernel.pak, arupa_kernel_resources.apk,
#                  icudtl.dat, snapshot_blob.bin,
#                  .arupa-version, .arupa-delivery-id}
#     kernel/x64/…（同上）
#     SHA256SUMS.txt / MANIFEST.md
#   例（历史参考包，件别=test，目录名不带 -static）：dist/arupa-android-154.0.8037.21+28-test
#
# 选项:
#       --os OS          android（默认；当前只做 android）
#       --arch ARCH      arm64 | x64 | all（默认 all）
#       --ver VER        版本号，默认读 src/chrome/VERSION
#   -o, --out-dir NAME  覆盖 out 目录名（默认 arupa-{os}-{arch}-{ver}-static）
#       --out PATH      直接指定 out 目录（优先级高于 -o，只支持单架构）
#   -n, --num N         交付序号（默认：已有 dist/arupa-{os}-{ver}-static-* 最大 n + 1）
#       --dist-dir DIR  交付根（默认 <repo>/dist）
#       --pak FILE      主 pak 来源；APP = 取 App 仓
#                       （../nomad_android/app/src/main/assets/arupa_kernel.pak）
#                       默认：out/content_shell.pak，否则 out 根体积最大的 *.pak
#   -p, --package DIR    附加交付内容源目录（默认 <repo>/package/package_android，整份并入交付包）
#       --no-package     不加 package/（只出内核件）
#       --docs DIR       额外把该目录整份拷成交付包里的 docs/；晚于 package/ 拷，
#                        同名内容以它为准（同理 --probe → probe-plugin/）
#       --probe DIR      额外拷成 probe-plugin/
#       --gate PATH     打包后跑 android_delivery_gate.py verify（默认不跑）
#   -z, --zip           额外打 dist/arupa-{os}-{ver}-static-{n}.zip
#   -f, --force         目标 dist 目录已存在时先删再打
#   -h, --help
#
# 复制映射（OUT = src/out/arupa-{os}-{arch}-{ver}-static）:
#   OUT/apks/arupa-kernel.aar                          → kernel/<arch>/arupa-kernel.aar
#   OUT/<主 pak>                                        → kernel/<arch>/arupa_kernel.pak
#   OUT/gen/chrome/browser/arupa_android/aar/arupa_kernel_resources.apk
#                                                      → kernel/<arch>/arupa_kernel_resources.apk
#   OUT/icudtl.dat                                     → kernel/<arch>/icudtl.dat
#   OUT/snapshot_blob.bin                              → kernel/<arch>/snapshot_blob.bin
#                                                        （另存 snapshot_blob_64.bin，gin 只认带后缀名）
#   OUT/v8_context_snapshot*.bin（有则带）              → 同名
#   版本标记                                            → kernel/<arch>/.arupa-version = ver
#                                                        kernel/<arch>/.arupa-delivery-id = ver+n
#   <repo>/package/package_android/<file>                               → <file>（交付根，同名）
#   <repo>/package/package_android/<dir>/…                              → <dir>/…（合并，含隐藏文件）
#
# 注意（照搬 dist/arupa-android-154.0.8037.21+28-test 时的几条硬约束）:
#   1. 资源 apk 取 gen/ 下那份「剥离版」（~1.1MB），不是 apks/ArupaKernelResources.apk（~30MB 全量）；
#      aar 内 assets/arupa_kernel_resources.apk 与它是同一份。
#   2. kernel/<arch>/ 平铺，不要再套 gen/ 之类的层级；外层目录名是 cpu（arm64/x64），
#      aar 内部必须是 Android ABI（arm64-v8a/x86_64）—— 打包后会核对，对不上直接失败。
#   3. arupa-kernel.aar 与 pak / icudtl / snapshot / resources.apk 版本锁死、同进同出。
#   4. 主 pak = out/content_shell.pak —— build_arupa.sh 的 android 默认目标已挂
#      content/shell:pak，正常编完就有。若 out 里没有（例如只编了 aar），先补跑
#      third_party/ninja/ninja -C out/<out> content/shell:pak，或用 --pak 指定。
# ==== 用法结束

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_DIR="${ROOT_DIR}/src"

OS="android"
ARCH="all"
VER=""
OUT_NAME=""
OUT_OVERRIDE=""
NUM=""
DIST_DIR="${ROOT_DIR}/dist"
PAK_SRC=""
PKG_DIR=""
DO_PACKAGE=1
DOCS_DIR=""
PROBE_DIR=""
GATE=""
DO_ZIP=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --os)              OS="${2:?--os 需要参数}"; shift 2 ;;
    --arch)            ARCH="${2:?--arch 需要参数}"; shift 2 ;;
    --ver)             VER="${2:?--ver 需要参数}"; shift 2 ;;
    -o|--out-dir)      OUT_NAME="${2:?--out-dir 需要参数}"; shift 2 ;;
    --out)             OUT_OVERRIDE="${2:?--out 需要参数}"; shift 2 ;;
    -n|--num)          NUM="${2:?--num 需要参数}"; shift 2 ;;
    --dist-dir)        DIST_DIR="${2:?--dist-dir 需要参数}"; shift 2 ;;
    --pak)             PAK_SRC="${2:?--pak 需要参数}"; shift 2 ;;
    -p|--package)      PKG_DIR="${2:?--package 需要参数}"; shift 2 ;;
    --no-package)      DO_PACKAGE=0; shift ;;
    --docs)            DOCS_DIR="${2:?--docs 需要参数}"; shift 2 ;;
    --probe)           PROBE_DIR="${2:?--probe 需要参数}"; shift 2 ;;
    --gate)            GATE="${2:?--gate 需要参数}"; shift 2 ;;
    -z|--zip)             DO_ZIP=1; shift ;;
    -f|--force)           FORCE=1; shift ;;
    -h|--help)         awk '/^# ==== 用法开始/{f=1;next} /^# ==== 用法结束/{f=0} f' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[[ -d "${SRC_DIR}" ]] || { echo "[x] 找不到源码树: ${SRC_DIR}" >&2; exit 1; }
[[ "${OS}" == "android" ]] || { echo "[x] 当前只支持 --os android: ${OS}" >&2; exit 1; }

log()  { echo "[+] $*"; }
warn() { echo "[!] $*" >&2; }
err()  { echo "[x] $*" >&2; exit 1; }

# ---------------------------------------------------------------- 版本号
if [[ -z "${VER}" ]]; then
  if [[ -f "${SRC_DIR}/chrome/VERSION" ]]; then
    VER="$( . "${SRC_DIR}/chrome/VERSION"
            printf "%s.%s.%s.%s" "${MAJOR:-0}" "${MINOR:-0}" "${BUILD:-0}" "${PATCH:-0}" )"
  fi
  [[ -n "${VER}" ]] || err "读不到版本号，请用 --ver 指定"
fi

# ---------------------------------------------------------------- 架构
case "${ARCH}" in
  all)     ARCHS=(arm64 x64) ;;
  arm64)   ARCHS=(arm64) ;;
  x64)     ARCHS=(x64) ;;
  *) err "--arch 只支持 arm64 | x64 | all: ${ARCH}" ;;
esac
[[ -z "${OUT_OVERRIDE}" || ${#ARCHS[@]} -eq 1 ]] || err "--out 只能用于单架构（--arch arm64|x64）"

abi_of_cpu() {
  case "$1" in
    arm64) echo arm64-v8a ;;
    x64)   echo x86_64 ;;
    *)     err "未知架构: $1" ;;
  esac
}

# ---------------------------------------------------------------- 交付序号 n
if [[ -z "${NUM}" ]]; then
  prefix="arupa-${OS}-${VER}-static-"
  max=0
  for d in "${DIST_DIR}/${prefix}"*; do
    [[ -d "${d}" ]] || continue
    n="${d##*${prefix}}"
    [[ "${n}" =~ ^[0-9]+$ ]] || continue
    (( n > max )) && max=$((n))
  done
  NUM=$((max+1))
fi
[[ "${NUM}" =~ ^[0-9]+$ ]] || err "--num 必须是数字: ${NUM}"

DELIVERY_ID="${VER}+${NUM}"
DIST="${DIST_DIR}/arupa-${OS}-${VER}-static-${NUM}"

if [[ -e "${DIST}" ]]; then
  if [[ "${FORCE}" -eq 1 ]]; then
    warn "目标已存在，--force 删除重建: ${DIST}"
    rm -rf "${DIST}"
  else
    err "目标已存在（加 --force 覆盖）: ${DIST}"
  fi
fi

log "交付包: ${DIST}"
log "交付 id: ${DELIVERY_ID}  （ver=${VER} n=${NUM}）"

# 中途失败别留下空壳 dist 目录（空壳会把下一次的序号顶上去，也会被误当成完整包）
trap 'st=$?; if [[ ${st} -ne 0 && -d "${DIST}" ]]; then rm -rf "${DIST}"; fi' EXIT

# ---------------------------------------------------------------- 公共附件

# package/：附加交付内容的唯一来源（交付文档、探针插件……）；默认必须存在
if [[ -z "${PKG_DIR}" ]]; then PKG_DIR="${ROOT_DIR}/package/package_android"; fi
if [[ "${DO_PACKAGE}" -eq 1 && ! -d "${PKG_DIR}" ]]; then
  err "找不到交付附件目录: ${PKG_DIR}"
fi

# 主 pak：把 Content 运行时资源与 Extensions renderer 模块合并。只复制
# content_shell.pak 会让 contextMenus / permissions 等自定义绑定在运行时
# 找不到 JS resource，API 虽出现在契约中却会令 Service Worker 注册失败。
# APP 仍表示显式复用 App 仓已验收的完整 pak。
resolve_pak() {
  local out="$1" f merged ext_generated ext_renderer ext_common ext_strings
  if [[ "${PAK_SRC}" == "APP" ]]; then
    PAK_SRC="${ROOT_DIR}/../nomad_android/app/src/main/assets/arupa_kernel.pak"
    [[ -f "${PAK_SRC}" ]] || err "--pak APP 找不到: ${PAK_SRC}"
    return
  fi
  if [[ -n "${PAK_SRC}" ]]; then
    [[ -f "${PAK_SRC}" ]] || err "--pak 找不到: ${PAK_SRC}"
    return
  fi
  if [[ -f "${out}/content_shell.pak" ]]; then
    ext_generated="${out}/gen/extensions/extensions_renderer_generated_resources.pak"
    ext_renderer="${out}/gen/extensions/extensions_renderer_resources.pak"
    ext_common="${out}/gen/extensions/extensions_resources.pak"
    # Android 只加载单一 arupa_kernel.pak，没有桌面 Chrome 独立的 locales/
    # 装载步骤。动态 content/user script 校验会通过 l10n_util 读取
    # IDS_EXTENSION_*；漏掉这份字符串包会在注册脚本时触发 ResourceBundle
    # CHECK，而不是返回可诊断的校验错误。
    ext_strings="${out}/gen/extensions/strings/extensions_strings_zh-CN.pak"
    [[ -f "${ext_generated}" ]] || err "缺 ${ext_generated}（Extensions renderer 自定义绑定资源未构建）"
    [[ -f "${ext_renderer}" ]] || err "缺 ${ext_renderer}（Extensions renderer 资源未构建）"
    [[ -f "${ext_common}" ]] || err "缺 ${ext_common}（Extensions 公共资源未构建）"
    [[ -f "${ext_strings}" ]] || err "缺 ${ext_strings}（Extensions 本地化字符串资源未构建）"
    merged="${out}/arupa_kernel.pak"
    python3 "${SRC_DIR}/tools/grit/pak_util.py" repack \
      "${merged}" \
      "${out}/content_shell.pak" \
      "${ext_generated}" \
      "${ext_renderer}" \
      "${ext_common}" \
      "${ext_strings}" \
      || err "合并 Content 与 Extensions 运行时资源失败"
    PAK_SRC="${merged}"
    return
  fi
  f="$(ls -1S "${out}"/*.pak 2>/dev/null | head -1 || true)"
  if [[ -n "${f}" && -f "${f}" ]]; then
    warn "out 里没有 content_shell.pak —— 取体积最大的 $(basename "${f}") 当主 pak"
    PAK_SRC="${f}"; return
  fi
  err "out 里没有 *.pak（${out}）—— 主 pak 缺失，内核加载 assets/arupa_kernel.pak 会失败。
    用 --pak <file> 指定，或 --pak APP 取 App 仓 assets/arupa_kernel.pak。"
}

# ---------------------------------------------------------------- 逐架构收件
for cpu in "${ARCHS[@]}"; do
  abi="$(abi_of_cpu "${cpu}")"
  if [[ -n "${OUT_OVERRIDE}" ]]; then
    OUT="${OUT_OVERRIDE}"
  else
    [[ -n "${OUT_NAME}" ]] && OUT="${SRC_DIR}/out/${OUT_NAME}" \
                           || OUT="${SRC_DIR}/out/arupa-${OS}-${cpu}-${VER}-static"
  fi
  [[ -d "${OUT}" ]] || err "没有 out 目录（先跑 scripts/build_arupa.sh --arch ${cpu}）: ${OUT}"
  PAK_SRC_SAVED="${PAK_SRC}"
  resolve_pak "${OUT}"

  dest="${DIST}/kernel/${cpu}"
  mkdir -p "${dest}"
  echo
  log "── kernel/${cpu} ← $(basename "${OUT}") ──"

  # AAR（唯一交付件：jni/<abi>/libarupakernel.so + classes.jar + assets/资源 apk）
  aar="${OUT}/apks/arupa-kernel.aar"
  [[ -f "${aar}" ]] || err "缺 ${aar}（ninja 目标 chrome/browser/arupa_android/aar:arupa_kernel_aar 没编）"
  cp -f "${aar}" "${dest}/arupa-kernel.aar"
  log "  arupa-kernel.aar            $(du -h "${aar}" | cut -f1)"

  # 主 pak
  cp -f "${PAK_SRC}" "${dest}/arupa_kernel.pak"
  log "  arupa_kernel.pak            $(du -h "${PAK_SRC}" | cut -f1)  ← $(basename "${PAK_SRC}")"

  # 资源 apk：gen/ 下那份剥离版（与 aar 内 assets/ 同一份），不是 apks/ 下的全量件
  rapk="${OUT}/gen/chrome/browser/arupa_android/aar/arupa_kernel_resources.apk"
  [[ -f "${rapk}" ]] || err "缺 ${rapk}（arupa_kernel_resources_apk_stripped 没编)"
  cp -f "${rapk}" "${dest}/arupa_kernel_resources.apk"
  log "  arupa_kernel_resources.apk  $(du -h "${rapk}" | cut -f1)"

  # icudtl / 快照
  for f in icudtl.dat; do
    [[ -f "${OUT}/${f}" ]] || err "缺 ${OUT}/${f}"
    cp -f "${OUT}/${f}" "${dest}/"
    log "  ${f}  $(du -h "${OUT}/${f}" | cut -f1)"
  done
  n_snap=0
  for f in "${OUT}"/snapshot_blob.bin "${OUT}"/v8_context_snapshot.bin \
           "${OUT}"/v8_context_snapshot_64.bin "${OUT}"/v8_context_snapshot_32.bin; do
    [[ -f "${f}" ]] || continue
    cp -f "${f}" "${dest}/"
    log "  $(basename "${f}")  $(du -h "${f}" | cut -f1)"
    n_snap=$((n_snap+1))
  done
  [[ ${n_snap} -gt 0 ]] || err "out 里没有 snapshot_blob*.bin / v8_context_snapshot*.bin —— 内核起不来"
  # arm64 内核 .so 编译期带 USE_V8_CONTEXT_SNAPSHOT=true，运行期 gin 按名字找 assets/
  # v8_context_snapshot_64.bin；缺它 App 一打开就 [FATAL:gin/v8_initializer.cc]（2026-09-28 事故）。
  if [[ "${cpu}" == "arm64" ]]; then
    [[ -f "${dest}/v8_context_snapshot_64.bin" ]] \
      || err "缺 ${dest}/v8_context_snapshot_64.bin（arm64 必需：构建时要带 ninja 目标 tools/v8_context_snapshot:generate_v8_context_snapshot）"
  fi
  # gin 在安卓上按指针宽度找 assets/snapshot_blob_{32,64}.bin，产出名没有后缀 —— 两份都给
  if [[ -f "${dest}/snapshot_blob.bin" ]]; then
    case "${cpu}" in
      arm64|x64) alias_name="snapshot_blob_64.bin" ;;
      *)         alias_name="snapshot_blob_32.bin" ;;
    esac
    [[ -f "${dest}/${alias_name}" ]] || cp -f "${dest}/snapshot_blob.bin" "${dest}/${alias_name}"
  fi


  # 版本标记
  printf '%s\n' "${VER}"         > "${dest}/.arupa-version"
  printf '%s\n' "${DELIVERY_ID}" > "${dest}/.arupa-delivery-id"
  log "  .arupa-version=${VER}  .arupa-delivery-id=${DELIVERY_ID}"

  # AAR 内部 ABI 必须等于 Android ABI（写错 AGP 静默不解包 .so，只在运行时炸）
  python3 - "${dest}/arupa-kernel.aar" "${abi}" <<'PY' || err "AAR 的 jni 目录与架构不匹配"
import sys, zipfile
aar, abi = sys.argv[1], sys.argv[2]
want = f'jni/{abi}/'
names = zipfile.ZipFile(aar).namelist()
so = [n for n in names if n.startswith('jni/') and n.endswith('.so')]
if not any(n.startswith(want) for n in so):
    print(f'[x] {aar} 里的 .so 是 {so or "空"}，期望 {want}*', file=sys.stderr)
    sys.exit(1)
print(f'  aar jni/{abi}/ ✓')
PY

  PAK_SRC="${PAK_SRC_SAVED}"   # 下一个架构重新解析（各架构的 pak 可能不同）
done

# ---------------------------------------------------------------- 契约反向核对（K-03/K-05）
# 不只看工作区源码：从每个 AAR 内解包 assets/arupa-kernel-contract.json，与源码派生契约
# 逐字段核对（陈旧/混装 AAR 在此被拒）；≥2 个架构时再比对跨 ABI 契约 digest/schema 一致。
aar_args=()
for cpu in "${ARCHS[@]}"; do
  aar_args+=(--aar "${DIST}/kernel/${cpu}/arupa-kernel.aar")
done
python3 "${ROOT_DIR}/scripts/kernel_capability.py" verify-aar "${aar_args[@]}" \
  || err "AAR 内嵌契约与源码不同源（K-03 反向核对 / K-05 跨 ABI 一致）：拒绝交付"
log "AAR 内嵌契约已核对（K-03 反向核对 + K-05 跨 ABI 一致）"

# ---------------------------------------------------------------- package/ 附加内容
# ⚠ 必须在算 SHA256SUMS.txt / MANIFEST.md 之前拷，否则清单里没有这些文件
if [[ "${DO_PACKAGE}" -eq 1 ]]; then
  mapfile -t pkg_entries < <(cd "${PKG_DIR}" && find . -maxdepth 1 -mindepth 1 -printf '%f\n' | LC_ALL=C sort)
  if [[ ${#pkg_entries[@]} -eq 0 ]]; then
    warn "package/ 是空的: ${PKG_DIR}"
  else
    echo
    log "── package/ ← ${PKG_DIR} ──"
    for name in "${pkg_entries[@]}"; do
      src="${PKG_DIR}/${name}"
      dst="${DIST}/${name}"
      if [[ -d "${src}" && ! -L "${src}" ]]; then
        mkdir -p "${dst}"
        cp -af "${src}/." "${dst}/"     # 合并进目标目录，含隐藏文件
        log "  ${name}/  （$(find "${src}" -type f | wc -l | tr -d ' ') 个文件）"
      elif [[ -f "${src}" || -L "${src}" ]]; then
        cp -af "${src}" "${dst}"
        log "  ${name}  $(du -h "${src}" | cut -f1)"
      else
        warn "  package/ 里跳过（既不是文件也不是目录）: ${name}"
      fi
    done
  fi
fi

# ---------------------------------------------------------------- 显式附件（覆盖 package/ 同名内容）
if [[ -n "${DOCS_DIR}" ]]; then
  [[ -d "${DOCS_DIR}" ]] || err "--docs 不是目录: ${DOCS_DIR}"
  mkdir -p "${DIST}/docs"; cp -r "${DOCS_DIR}/." "${DIST}/docs/"
  log "附带 docs/ ← ${DOCS_DIR}"
fi
if [[ -n "${PROBE_DIR}" ]]; then
  [[ -d "${PROBE_DIR}" ]] || err "--probe 不是目录: ${PROBE_DIR}"
  mkdir -p "${DIST}/probe-plugin"; cp -r "${PROBE_DIR}/." "${DIST}/probe-plugin/"
  log "附带 probe-plugin/ ← ${PROBE_DIR}"
fi

# ---------------------------------------------------------------- 能力清单（声明 vs 实现同源门禁）
# 清单**必须由内核源码派生**：schema/路由/权限/事件/内核分发表/窗口预算常量/跨语言结果码/
# consumer keep 规则/无假保活，全部双向核对（scripts/kernel_capability.py）。
# 不一致就 exit 1 拒绝出包 —— 不允许"清单声明 passed、实现没有"（N-C08/K-A09）。
# 必须在算 SHA256SUMS.txt 之前生成，否则清单自己不在校验和里。
python3 "${ROOT_DIR}/scripts/kernel_capability.py" manifest \
    --stage "${DIST}" --ver "${VER}" --num "${NUM}" \
  || err "能力清单生成失败：内核声明与实现不同源，拒绝出包（见上面 [ERROR]）"
log "capability-manifest.json（由内核源码派生；同源门禁已通过）"

# ---------------------------------------------------------------- 校验和 / 清单
# ⚠ 临时文件必须落在交付目录外：放在 DIST 里会被 find 自己算进去
#   （且 grep -v '^SHA256SUMS.txt' 删不掉——sha256sum 的行是「哈希  文件」开头）
sums_tmp="$(mktemp "${TMPDIR:-/tmp}/arupa-sums.XXXXXX")"
( cd "${DIST}" && find . -type f ! -path './SHA256SUMS.txt' ! -path './MANIFEST.md' -print0 \
    | LC_ALL=C sort -z | while IFS= read -r -d '' f; do sha256sum "${f#./}"; done ) > "${sums_tmp}"
{
  echo "# sha256  文件  （相对交付根，本目录下的清单文件不在其中）"
  echo "# 交付 id ${DELIVERY_ID} · 打包 $(date '+%Y-%m-%d %H:%M:%S')"
  cat "${sums_tmp}"
} > "${DIST}/SHA256SUMS.txt"
rm -f "${sums_tmp}"
log "SHA256SUMS.txt（$(grep -c . "${DIST}/SHA256SUMS.txt") 行）"

{
  echo "# Android 内核交付清单"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| 交付 id | \`${DELIVERY_ID}\` |"
  echo "| 版本 | \`${VER}\` |"
  echo "| 架构 | $(IFS=, ; echo "${ARCHS[*]}") |"
  echo "| 打包时间 | $(date '+%Y-%m-%d %H:%M:%S') |"
  echo
  echo "## 交付件 sha256"
  for cpu in "${ARCHS[@]}"; do
    ( cd "${DIST}" && find "kernel/${cpu}" -type f -print | LC_ALL=C sort | while IFS= read -r f; do
        echo "- \`${f#kernel/${cpu}/}\`（${cpu}）  $(sha256sum "${f}" | cut -d' ' -f1)"
      done )
  done
  echo
  echo "## 附加内容 sha256（package/）"
  # 顶层除 kernel/ 和两个清单外的所有条目（package/ 拷进来的就在这里）
  # ⚠ 用 find 自己排除，不要接 grep -v：pipefail 下 grep 空输入返回 1 会直接把脚本打断
  ( cd "${DIST}" && find . -maxdepth 1 -mindepth 1 \
        ! -name kernel ! -name SHA256SUMS.txt ! -name MANIFEST.md -printf '%f\n' \
      | LC_ALL=C sort | while IFS= read -r f; do
          if [[ -d "${f}" ]]; then
            find "${f}" -type f -print | LC_ALL=C sort | while IFS= read -r g; do
              echo "- \`${g}\`  $(sha256sum "${g}" | cut -d' ' -f1)"
            done
          elif [[ -f "${f}" ]]; then
            echo "- \`${f}\`  $(sha256sum "${f}" | cut -d' ' -f1)"
          fi
        done )
  } > "${DIST}/MANIFEST.md"
log "MANIFEST.md"

# ---------------------------------------------------------------- 门禁（可选）
if [[ -n "${GATE}" ]]; then
  [[ -f "${GATE}" ]] || err "--gate 找不到: ${GATE}"
  log "跑交付门禁: ${GATE} verify"
  python3 "${GATE}" verify "${DIST}" || err "交付门禁未通过"
fi

# ---------------------------------------------------------------- zip（可选）
if [[ "${DO_ZIP}" -eq 1 ]]; then
  zip_path="${DIST}.zip"
  rm -f "${zip_path}"
  ( cd "${DIST_DIR}" && zip -q -r "$(basename "${zip_path}")" "$(basename "${DIST}")" ) \
    || err "打 zip 失败（需要 zip 命令）"
  log "zip: ${zip_path}"
fi

echo
echo "[✓] 完成: ${DIST}"
for cpu in "${ARCHS[@]}"; do
  ( cd "${DIST}/kernel/${cpu}" && du -sh . | cut -f1 | xargs -I{} echo "    kernel/${cpu}  {}" )
done
