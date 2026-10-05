#!/usr/bin/env bash
# ==== 用法开始
# 把 src/out/arupa-{os}-{arch}-{ver}-static 里的桌面端内核件整理成交付包：
#     dist/arupa-{os}-{arch}-{ver}-static-{n}/
#
# 用法:
#   scripts/package-arupa-desktop.sh [选项]
#
# 输出目录名（目录自带 arch，所以每个包的 kernel/ 是平铺的，不再按架构分层）：
#   dist/arupa-mac-arm64-154.0.8037.21-static-1/
#     kernel/{libarupa_kernel.dylib, render, *.pak, icudtl.dat, snapshot_blob.bin,
#             v8_context_snapshot.arm64.bin, libEGL.dylib, libGLESv2.dylib, …,
#             angledata/, hyphen-data/, resources/, plugin-runtime/,
#             .arupa-version, .arupa-delivery-id}
#     include/{arupa_kernel_capi.h, arupa_kernel_capi_nomad.h}
#     docs/ dotnet/ …        ← <repo>/package 下的一级文件/文件夹整份搬过来（有则带）
#     macKernel -> kernel     ← 仅 mac：PC 侧按 macKernel/ 取内核件（软链，勿实体复制）
#     SHA256SUMS.txt / MANIFEST.md
#   linux 同理，库名 libarupa_kernel.so，arupa_render 同样为必需件
#
# 参考: dist/arupa-win-154.0.8037.21+17（Windows 交付包的目录与收件口径）
#
# 选项:
#       --os OS          mac | linux（默认按宿主：Darwin -> mac，Linux -> linux）
#       --arch ARCH      x86 | x64 | arm64 | all（默认按宿主架构；all = 两种架构各出一个包，共用同一 n）
#       --ver VER        版本号，默认读 src/chrome/VERSION
#   -o, --out-dir NAME  覆盖 out 目录名（默认 arupa-{os}-{arch}-{ver}-static，只支持单架构）
#       --out PATH      直接指定 out 目录（优先级高于 -o，只支持单架构）
#   -n, --num N         交付序号（默认：dist/arupa-{os}-{arch}-{ver}-static-* 最大 n + 1）
#       --dist-dir DIR  交付根（默认 <repo>/dist）
#       --pak FILE      主 pak 来源，默认 out/content_shell.pak，
#                       没有时取 out 根体积最大的 *.pak
#       --include-dir DIR       默认 <repo>/arupa_desktop/public（里面的 *.h 拷进 include/）
#       --package-dir DIR       把该目录下的一级文件/文件夹整份拷进交付根
#                               （默认 <repo>/package/package_desktop，常见: docs/ dotnet/）
#       --no-package    不拷 package 目录
#       --docs DIR      额外把该目录整份拷成 docs/
#       --probe DIR     额外拷成 probe-plugin/
#   -z, --zip           额外打 dist/arupa-{os}-{arch}-{ver}-static-{n}.zip（mac 用 zip -y 保软链）
#   -f, --force         目标 dist 目录已存在时先删再打
#   -h, --help
#
# 复制映射（OUT = src/out/arupa-{os}-{arch}-{ver}-static）:
#   OUT/libarupa_kernel.dylib | .so              → kernel/（内核本体，必需）
#   OUT/arupa_render                             → kernel/arupa_render（渲染进程薄壳，必需/仅告警）
#   OUT/<主 pak>                                  → kernel/arupa_kernel.pak 之外的原名保留：
#                                                  content_shell.pak 等 *.pak 按原名平铺
#   OUT/icudtl.dat                               → kernel/icudtl.dat（必需）
#   OUT/snapshot_blob.bin / v8_context_snapshot* → kernel/同名（至少一个，必需）
#   OUT/libEGL.* / libGLESv2.* / libvk_swiftshader.* / libvulkan.*  → kernel/（有则带）
#   OUT/angledata/ hyphen-data/ resources/          → kernel/同名目录（可选）
#   OUT/locales/{en-US,zh-CN}.pak                 → kernel/locales/（旧构建可无；有则两者必需）
#   OUT/vk_swiftshader_icd.json                  → kernel/（有则带）
#   OUT/devtools_resources.pak 或 OUT/gen/content/browser/devtools/devtools_resources.pak → kernel/（必需）
#   OUT/Libraries/libtest_trace_processor.dylib  → kernel/Libraries/（mac，有则带）
#   <repo>/arupa_desktop/public/*.h              → include/…
#   <repo>/package/package_desktop/{docs,dotnet,…}               → 交付根同名（有则带，跟 scripts/builder/kernel.py
#                                                  的 copy_assets 同一口径）
#   <repo>/package/package_desktop/plugin-runtime/               → kernel/plugin-runtime/（运行期按「内核目录/
#                                                  plugin-runtime/nomad-plugin-runtime.js」取，不放交付根）
#   版本标记                                     → kernel/.arupa-version = ver
#                                                  kernel/.arupa-delivery-id = ver+n
#   mac 专属                                     → macKernel -> kernel（软链；PC 侧 Mac/ArupaDelivery.props
#                                                  与 Directory.Build.targets 按 macKernel/ 取件）
#
# 校验:
#   * 必缺少 -> 直接失败，并且不留半成品 dist 目录（免得把下次的序号顶上去）
#   * 可选件缺失 -> 打印 [!] 告警汇总，不阻断
#   * 收尾用 file(mac) / readelf(linux) 核对内核库的 CPU 架构与本次打包的 arch 是否一致
# ==== 用法结束

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SRC_DIR="${ROOT_DIR}/src"

OS=""
ARCH=""
VER=""
OUT_NAME=""
OUT_OVERRIDE=""
NUM=""
DIST_DIR="${ROOT_DIR}/dist"
PAK_SRC=""
INC_DIR=""
PKG_DIR=""
DOCS_DIR=""
PROBE_DIR=""
DO_ZIP=0
NO_PACKAGE=0
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
    --include-dir)     INC_DIR="${2:?--include-dir 需要参数}"; shift 2 ;;
    --package-dir)     PKG_DIR="${2:?--package-dir 需要参数}"; shift 2 ;;
    --no-package)      NO_PACKAGE=1; shift ;;
    --docs)            DOCS_DIR="${2:?--docs 需要参数}"; shift 2 ;;
    --probe)           PROBE_DIR="${2:?--probe 需要参数}"; shift 2 ;;
    -z|--zip)          DO_ZIP=1; shift ;;
    -f|--force)        FORCE=1; shift ;;
    -h|--help)         awk '/^# ==== 用法开始/{f=1;next} /^# ==== 用法结束/{f=0} f' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[[ -d "${SRC_DIR}" ]] || { echo "[x] 找不到源码树: ${SRC_DIR}" >&2; exit 1; }

log()  { echo "[+] $*"; }
warn() { echo "[!] $*" >&2; }
err()  { echo "[x] $*" >&2; exit 1; }

# ---------------------------------------------------------------- 平台 / 架构
norm_cpu() {
  case "$1" in
    arm64|aarch64) echo arm64 ;;
    x64|x86_64|amd64) echo x64 ;;
    *) echo "$1" ;;
  esac
}

case "$(uname -s)" in
  Darwin) HOST_OS="mac" ;;
  Linux)  HOST_OS="linux" ;;
  *) echo "[x] 本脚本只支持 macOS / Linux 宿主: $(uname -s)" >&2; exit 1 ;;
esac

[[ -n "${OS}" ]] || OS="${HOST_OS}"
case "${OS}" in
  mac|linux) ;;
  *) err "--os 只支持 mac 或 linux: ${OS}" ;;
esac

[[ -n "${ARCH}" ]] || ARCH="$(norm_cpu "$(uname -m)")"
case "${ARCH}" in
  all)    if [[ "${OS}" == "linux" ]]; then ARCHS=(x86 x64 arm64); else ARCHS=(x64 arm64); fi ;;
  x86)    [[ "${OS}" == "linux" ]] || err "mac 不支持 x86"; ARCHS=(x86) ;;
  arm64)  ARCHS=(arm64) ;;
  x64)    ARCHS=(x64) ;;
  *) err "--arch 只支持 x86 | x64 | arm64 | all: ${ARCH}" ;;
esac
[[ -z "${OUT_OVERRIDE}" && -z "${OUT_NAME}" || ${#ARCHS[@]} -eq 1 ]] \
  || err "-o/--out 只能用于单架构（--arch arm64|x64）"

lib_name()   { [[ "$1" == "mac" ]] && echo "libarupa_kernel.dylib" || echo "libarupa_kernel.so"; }
helper_name() { echo "arupa_render"; }

# ---------------------------------------------------------------- 版本号
if [[ -z "${VER}" ]]; then
  if [[ -f "${SRC_DIR}/chrome/VERSION" ]]; then
    VER="$( . "${SRC_DIR}/chrome/VERSION"
            printf "%s.%s.%s.%s" "${MAJOR:-0}" "${MINOR:-0}" "${BUILD:-0}" "${PATCH:-0}" )"
  fi
  [[ -n "${VER}" ]] || err "读不到版本号，请用 --ver 指定"
fi

# ---------------------------------------------------------------- 交付序号 n
if [[ -z "${NUM}" ]]; then
  max=0
  for cpu in "${ARCHS[@]}"; do
    prefix="arupa-${OS}-${cpu}-${VER}-static-"
    for d in "${DIST_DIR}/${prefix}"*; do
      [[ -d "${d}" ]] || continue
      n="${d##*${prefix}}"
      [[ "${n}" =~ ^[0-9]+$ ]] || continue
      (( n > max )) && max=$((n))
    done
  done
  NUM=$((max+1))
fi
[[ "${NUM}" =~ ^[0-9]+$ ]] || err "--num 必须是数字: ${NUM}"

DELIVERY_ID="${VER}+${NUM}"

# sha256 工具：macOS 没有 sha256sum
if command -v sha256sum >/dev/null 2>&1; then
  sha_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  err "找不到 sha256sum / shasum"
fi

# ---------------------------------------------------------------- 公共附件
if [[ -z "${INC_DIR}" ]]; then INC_DIR="${ROOT_DIR}/arupa_desktop/public"; fi

# package 附加件（docs/ dotnet/ …）：一级文件/文件夹整份搬进交付根，与 kernel.py copy_assets 同口径
if [[ -z "${PKG_DIR}" ]]; then PKG_DIR="${ROOT_DIR}/package/package_desktop"; fi
if [[ "${NO_PACKAGE}" -eq 1 ]]; then
  PKG_DIR=""
else
  [[ -d "${PKG_DIR}" ]] || err "找不到交付附件目录: ${PKG_DIR}"
fi

# 主 pak：out 根 content_shell.pak，没有则体积最大的 *.pak
resolve_pak() {
  local out="$1" f
  if [[ -n "${PAK_SRC}" ]]; then
    [[ -f "${PAK_SRC}" ]] || err "--pak 找不到: ${PAK_SRC}"
    return
  fi
  if [[ -f "${out}/content_shell.pak" ]]; then
    PAK_SRC="${out}/content_shell.pak"; return
  fi
  f="$(ls -1S "${out}"/*.pak 2>/dev/null | head -1 || true)"
  if [[ -n "${f}" && -f "${f}" ]]; then
    warn "out 里没有 content_shell.pak —— 取体积最大的 $(basename "${f}") 当主 pak"
    PAK_SRC="${f}"; return
  fi
  err "out 里没有 *.pak（${out}）—— 资源 pak 缺失，内核起来也会缺资源。
    用 --pak <file> 指定，或先跑 scripts/build-arupa-desktop.sh 把 pak 目标编出来。"
}

want() {   # 必需件
  local src="$1" dst="$2"
  [[ -f "${src}" ]] || err "缺必需件: ${src}（$3）"
  cp -f "${src}" "${dst}/"
  log "  $(basename "${dst}/$(basename "${src}")")  $(du -h "${src}" | cut -f1)"
}
have() {   # 可选件
  local src="$1" dst="$2"
  [[ -f "${src}" ]] || { MISSING_OPTIONAL+=("$(basename "${src}")"); return; }
  cp -f "${src}" "${dst}/"
  log "  $(basename "${src}")  $(du -h "${src}" | cut -f1)"
}
have_dir() {
  local src="$1" dst="$2"
  [[ -d "${src}" ]] || { MISSING_OPTIONAL+=("$(basename "${src}")/"); return; }
  cp -R "${src}" "${dst}/"
  log "  $(basename "${src}")/  $(du -sh "${src}" | cut -f1)"
}

# 把 <repo>/package 下的一级文件/文件夹整份搬进交付根（docs/ dotnet/ …）
# 口径同 scripts/builder/kernel.py 的 copy_assets：平台无关件不在 kernel/ 里，
# 直接平铺在交付根，宿主按 dist/docs、dist/dotnet 取用。
copy_package_dir() {
  local dist="$1" e name n=0
  [[ -n "${PKG_DIR}" ]] || return 0       # --no-package 或目录不存在
  [[ -d "${PKG_DIR}" ]] || return 0

  local entries=()
  for e in "${PKG_DIR}"/* "${PKG_DIR}"/.[!.]* "${PKG_DIR}"/..?*; do
    [[ -e "${e}" || -L "${e}" ]] || continue           # 没匹配上的 glob 原样留着，跳过
    name="$(basename "${e}")"
    [[ "${name}" == ".DS_Store" ]] && continue
    entries+=("${e}")
  done

  if [[ ${#entries[@]} -eq 0 ]]; then
    warn "package 目录是空的，交付包里不会有 docs/ dotnet/: ${PKG_DIR}"
    return 0
  fi

  for e in "${entries[@]}"; do
    name="$(basename "${e}")"
    # 撞名就停：静默覆盖（尤其 --docs/--probe 已经建了同名目录）会混出半新半旧的目录
    [[ ! -e "${dist}/${name}" ]] \
      || err "package 附加件与交付目录里已有的 ${name} 撞名（${dist}/${name}）—— 用 --package-dir 换个来源，或去掉 --docs/--probe"
    if [[ -d "${e}" ]]; then
      cp -R "${e}" "${dist}/"
      log "  ${name}/  ← package/${name}  $(du -sh "${e}" | cut -f1)"
    else
      cp -f "${e}" "${dist}/"
      log "  ${name}  ← package/${name}  $(du -h "${e}" | cut -f1)"
    fi
    n=$((n+1))
  done
  log "  package 附加 ${n} 项 ← ${PKG_DIR}"
}

# ---------------------------------------------------------------- 单个包的收件
package_arch() {
  local cpu="$1" out="$2" dist="$3"
  local dest="${dist}/kernel"
  MISSING_OPTIONAL=()

  PAK_SRC_SAVED="${PAK_SRC}"
  resolve_pak "${out}"

  mkdir -p "${dest}"
  echo
  log "── ${OS}/${cpu} ← $(basename "${out}") ──"

  # 内核本体
  want "${out}/$(lib_name "${OS}")" "${dest}" "ninja 目标 chrome/browser/arupa_desktop:arupa_kernel"
  # 渲染进程薄壳：mac/linux 都依赖它启动 renderer / GPU / utility 子进程
  if [[ -f "${out}/$(helper_name)" ]]; then
    want "${out}/$(helper_name)" "${dest}" ""
  else
    err "缺 ${out}/arupa_render（ninja 目标 chrome/browser/arupa_desktop:render，产物名 output_name=arupa_render，少了它没有渲染进程）"
  fi

  # 主 pak
  want "${out}/arupa_plugin_host" "${dest}" "ninja 目标 chrome/browser/arupa_desktop:arupa_plugin_host"
  want "${PAK_SRC}" "${dest}" ""
  want "${out}/devtools_resources.pak" "${dest}" "ninja 目标 chrome/browser/arupa_desktop:arupa_devtools_resources"

  # 原生宿主（可选形态）：内核静态链进 arupa_desktop，子进程由同一可执行文件经 --type=
  # 分流拉起。macOS/Linux 的沙箱（Seatbelt / Linux 命名空间）把策略交给子进程自己套用，
  # 没有跨进程内存写，libarupa_kernel.{dylib,so} 的库式嵌入本来就是安全形态 —— 所以这一件
  # **不是必需件**：构建图里带了就随包（下游装配层 kernel/ 里见到它就切原生宿主装配），
  # 没带就维持库式嵌入。三平台同一套判据，不按平台硬编。
  local cand
  for cand in arupa_desktop arupa_desktop.exe; do
    if [[ -f "${out}/${cand}" ]]; then
      want "${out}/${cand}" "${dest}" "ninja 目标 chrome/browser/arupa_desktop:arupa_desktop（原生宿主形态）"
      log "  原生宿主形态: 随包带 ${cand}，下游会据此改走原生宿主装配"
      break
    fi
  done

  # 其余 *.pak（ui_resources / extensions_* 等）：必须有这些 UI 资源，缺 warn
  local f any_pak=0
  for f in "${out}"/*.pak; do
    [[ -f "${f}" ]] || continue
    any_pak=1
    [[ -f "${dest}/$(basename "${f}")" ]] && continue
    have "${f}" "${dest}"
  done
  [[ ${any_pak} -eq 1 ]] || err "out 里没有任何 *.pak: ${out}"

  # Chromium leaves extension/component packs under gen/. The runtime uses the
  # same relative paths as a fallback, so root-only pak collection is incomplete.
  local relative gen_paks=0
  if [[ -d "${out}/gen" ]]; then
    while IFS= read -r -d '' f; do
      relative="${f#"${out}/"}"
      mkdir -p "${dest}/$(dirname "${relative}")"
      cp -f "${f}" "${dest}/${relative}"
      gen_paks=$((gen_paks+1))
    done < <(find "${out}/gen" -type f -name '*.pak' -print0)
  fi
  log "  gen/**/*.pak: ${gen_paks} 个（保留运行时相对路径）"
  for relative in gen/extensions/strings/extensions_strings_en-US.pak \
                  gen/extensions/extensions_renderer_generated_resources.pak; do
    [[ -f "${dest}/${relative}" || -f "${dest}/$(basename "${relative}")" ]] \
      || err "缺必需的扩展资源: ${relative}（请先构建内核资源）"
  done

  # icudtl / 快照
  want "${out}/icudtl.dat" "${dest}" ""
  local n_snap=0
  for f in "${out}"/snapshot_blob.bin "${out}"/v8_context_snapshot*.bin; do
    [[ -f "${f}" ]] || continue
    have "${f}" "${dest}"
    n_snap=$((n_snap+1))
  done
  [[ ${n_snap} -gt 0 ]] || err "out 里没有 snapshot_blob.bin / v8_context_snapshot*.bin —— 内核起不来: ${out}"

  # ANGLE / SwiftShader / Vulkan
  if [[ "${OS}" == "mac" ]]; then
    for f in libEGL.dylib libGLESv2.dylib libvk_swiftshader.dylib libvulkan.dylib \
             libVkICD_mock_icd.dylib libVkLayer_khronos_validation.dylib; do
      have "${out}/${f}" "${dest}"
    done
  else
    for f in libEGL.so libGLESv2.so libvk_swiftshader.so libvulkan.so.1 libvulkan.so \
             libVkICD_mock_icd.so libVkLayer_khronos_validation.so; do
      have "${out}/${f}" "${dest}"
    done
  fi

  # 数据目录
  if [[ "${OS}" == "mac" ]]; then
    [[ -f "${out}/hyphen-data/manifest.json" ]] || err "缺少 ${out}/hyphen-data/manifest.json；请先执行 build.sh arupa_desktop gen build --os mac --arch ${cpu}"
    compgen -G "${out}/hyphen-data/*.hyb" >/dev/null || err "缺少 ${out}/hyphen-data/*.hyb；请构建 third_party/hyphenation-patterns:bundle_hyphen_data"
  fi
  for d in angledata hyphen-data resources; do
    have_dir "${out}/${d}" "${dest}"
  done

  # Older content-shell deliveries embed English strings in the main pak.
  # Nomad supports Simplified Chinese and English. Copy an explicit allowlist
  # so stale outputs from previous multilingual builds cannot enter deliveries.
  if [[ -d "${out}/locales" ]]; then
    mkdir -p "${dest}/locales"
    local locale
    for locale in en-US zh-CN; do
      [[ -s "${out}/locales/${locale}.pak" ]] || err "缺少或为空: locales/${locale}.pak；请先构建 arupa_locales（中英文均为必需）"
      want "${out}/locales/${locale}.pak" "${dest}/locales" "arupa_locales"
    done
  else
    log "  locales/: 旧构建未生成独立语言包，使用主 pak 的英文资源"
  fi

  # 与 scripts/builder/kernel.py 的 ARTIFACTS[<os>].optional 同口径的散件。
  # PC 侧 Mac/ArupaDelivery.props 会逐件校验（缺一件就拦发布），所以宁可这里带全：
  #   vk_swiftshader_icd.json  Vulkan/SwiftShader ICD 描述，缺了软件渲染回退起不来
  #   devtools_resources.pak   devtools 前端资源
  #   Libraries/libtest_trace_processor.dylib  perfetto trace processor
  have "${out}/vk_swiftshader_icd.json" "${dest}"
  if [[ -f "${out}/devtools_resources.pak" ]]; then
    have "${out}/devtools_resources.pak" "${dest}"
  else
    # Chromium 侧 devtools 资源只在 gen/ 下产出（chrome/ 的 bundle_data 才会拷到 out 根）
    have "${out}/gen/content/browser/devtools/devtools_resources.pak" "${dest}"
  fi
  if [[ "${OS}" == "mac" ]]; then
    mkdir -p "${dest}/Libraries"
    have "${out}/Libraries/libtest_trace_processor.dylib" "${dest}/Libraries"
  fi



  # include/
  if [[ -d "${INC_DIR}" ]]; then
    mkdir -p "${dist}/include"
    local h n_hdr=0
    for h in "${INC_DIR}"/*.h; do
      [[ -f "${h}" ]] || continue
      cp -f "${h}" "${dist}/include/"
      n_hdr=$((n_hdr+1))
    done
    [[ ${n_hdr} -gt 0 ]] || warn "--include-dir 下没有 *.h: ${INC_DIR}"
  else
    warn "找不到 C API 头文件目录: ${INC_DIR}（可用 --include-dir 指定）"
  fi

  # 版本标记
  printf '%s\n' "${VER}"         > "${dest}/.arupa-version"
  printf '%s\n' "${DELIVERY_ID}" > "${dest}/.arupa-delivery-id"
  log "  .arupa-version=${VER}  .arupa-delivery-id=${DELIVERY_ID}"

  # 可选附件
  if [[ -n "${DOCS_DIR}" ]]; then
    [[ -d "${DOCS_DIR}" ]] || err "--docs 不是目录: ${DOCS_DIR}"
    mkdir -p "${dist}/docs"; cp -R "${DOCS_DIR}/." "${dist}/docs/"
    log "  附带 docs/ ← ${DOCS_DIR}"
  fi
  if [[ -n "${PROBE_DIR}" ]]; then
    [[ -d "${PROBE_DIR}" ]] || err "--probe 不是目录: ${PROBE_DIR}"
    mkdir -p "${dist}/probe-plugin"; cp -R "${PROBE_DIR}/." "${dist}/probe-plugin/"
    log "  附带 probe-plugin/ ← ${PROBE_DIR}"
  fi

  copy_package_dir "${dist}"

  # 交付根下的 plugin-runtime/ 归位到 kernel/：运行期按「内核目录/plugin-runtime/nomad-plugin-runtime.js」
  # 取运行时（NomadBrowser 的 _kernel/plugin-runtime 路由、Mac bundle 的 $(ArupaPluginRuntimeDir)），
  # scripts/builder/kernel.py 的 _copy_kernel_assets 同样是落在 kernel/plugin-runtime。
  if [[ -d "${dist}/plugin-runtime" ]]; then
    mkdir -p "${dest}"
    rm -rf "${dest}/plugin-runtime"
    mv "${dist}/plugin-runtime" "${dest}/plugin-runtime"
    log "  plugin-runtime/ -> kernel/plugin-runtime/"
  fi

  # Mac: PC 侧按 macKernel/ 取内核件（Mac/ArupaDelivery.props、Directory.Build.targets 读
  # macKernel/.arupa-version），交付根必须同时能看到 kernel/ 与 macKernel —— kernel.py 同规矩。
  if [[ "${OS}" == "mac" ]]; then
    rm -f "${dist}/macKernel"
    ln -s kernel "${dist}/macKernel"
    log "  软链 macKernel -> kernel（PC 侧按此名取内核）"
  fi

  find "${dist}" -name '.DS_Store' -delete 2>/dev/null || true

  # 架构核对：内核库必须就是本次打包的 arch
  check_lib_arch "${dest}/$(lib_name "${OS}")" "${cpu}"

  if [[ ${#MISSING_OPTIONAL[@]} -gt 0 ]]; then
    warn "可选件缺失 ${#MISSING_OPTIONAL[@]} 项（不阻断）: ${MISSING_OPTIONAL[*]}"
  fi

  # Mac: PC 侧 NomadBrowser.Mac.KernelHost 直接**编译**交付根 dotnet/ 下的 wrapper 源
  # （工程里写死的 C6 裁定：glob Interop.cs + ArupaBrowser.cs，不引锁平台的 ArupaKernel.csproj），
  # 而 wrapper 源只随内核仓的 wrapper 包发（内核仓没有远端，clone 不到）。缺了 PC 发布/打包
  # 第一步就被 Mac/ArupaDelivery.props 拦下 —— 出包时先说清楚，别等 PC 那边报。
  if [[ "${OS}" == "mac" ]]; then
    for f in ArupaBrowser.cs Interop.cs; do
      [[ -f "${dist}/dotnet/${f}" ]] \
        || warn "交付根 dotnet/${f} 缺失：PC 侧 mac 发布会拦（wrapper 与 macKernel 必须同一份交付）。把内核 wrapper 包里的 ArupaBrowser.cs / Interop.cs / ArupaKernel.csproj 放进 ${PKG_DIR}/dotnet/ 后重新出包"
    done
  fi

  PAK_SRC="${PAK_SRC_SAVED}"   # 下一个架构重新解析
}

check_lib_arch() {
  local lib="$1" want_cpu="$2" got=""
  if [[ "${OS}" == "mac" ]]; then
    command -v file >/dev/null 2>&1 || { warn "没有 file 命令，跳过架构核对"; return 0; }
    got="$(file -b "${lib}")"
    case "${want_cpu}" in
      arm64) [[ "${got}" == *arm64* ]]  || { err "内核库架构对不上（期望 arm64）: ${got}"; return 0; } ;;
      x64)   [[ "${got}" == *x86_64* ]] || { err "内核库架构对不上（期望 x86_64）: ${got}"; return 0; } ;;
    esac
    log "  架构核对 ✓ ${want_cpu}"
  else
    command -v readelf >/dev/null 2>&1 || { warn "没有 readelf，跳过架构核对"; return 0; }
    got="$(LC_ALL=C readelf -h "${lib}" | sed -n 's/.*Machine:[[:space:]]*//p')"
    case "${want_cpu}" in
      x86) [[ "${got}" == *80386* ]] || err "内核库架构对不上（期望 x86，实际 ${got}）" ;;
      arm64) [[ "${got}" == "AArch64" ]] || { err "内核库架构对不上（期望 AArch64，实际 ${got}）"; return 0; } ;;
      x64)   [[ "${got}" == *X86-64* ]]  || { err "内核库架构对不上（期望 X86-64，实际 ${got}）"; return 0; } ;;
    esac
    log "  架构核对 ✓ ${want_cpu}"
  fi
}

write_sums_and_manifest() {
  local dist="$1" cpu="$2"
  # tmp 放 dist 外面：否则会被自己的 find 扫进去
  local tmp n_items
  tmp="$(mktemp "${TMPDIR:-/tmp}/arupa-sums.XXXXXX")"
  ( cd "${dist}" && find . -type f -print0 | LC_ALL=C sort -z \
      | while IFS= read -r -d '' f; do echo "$(sha_of "${f#./}")  ${f#./}"; done \
      | grep -v -E '(SHA256SUMS\.txt|MANIFEST\.md)$' ) > "${tmp}"
  {
    echo "# sha256  文件  （相对交付根，本目录下的清单文件不在其中）"
    echo "# 交付 id ${DELIVERY_ID} · 打包 $(date '+%Y-%m-%d %H:%M:%S')"
    cat "${tmp}"
  } > "${dist}/SHA256SUMS.txt"
  n_items="$(wc -l < "${tmp}" | tr -d ' ')"
  rm -f "${tmp}"
  log "SHA256SUMS.txt（${n_items} 项）"

  {
    echo "# Arupa 内核 · ${OS}(${cpu}) 交付清单"
    echo
    echo "| | |"
    echo "|---|---|"
    echo "| 交付 id | \`${DELIVERY_ID}\` |"
    echo "| 内核版本 | \`${VER}\` |"
    echo "| 平台 | \`${OS}\` / \`${cpu}\` |"
    echo "| 来源 out | \`$(basename "${OUT_USED}")\` |"
    echo "| 打包时间 | $(date '+%Y-%m-%d %H:%M:%S') |"
    echo
    echo "## 交付件 sha256"
    echo
    ( cd "${dist}" && find kernel -type f -print | LC_ALL=C sort | while IFS= read -r f; do
        echo "- \`${f}\`  \`$(sha_of "${f}")\`"
      done )
  } > "${dist}/MANIFEST.md"
  log "MANIFEST.md"
}

# ---------------------------------------------------------------- 逐架构打包
for cpu in "${ARCHS[@]}"; do
  if [[ -n "${OUT_OVERRIDE}" ]]; then
    OUT="${OUT_OVERRIDE}"
  elif [[ -n "${OUT_NAME}" ]]; then
    OUT="${SRC_DIR}/out/${OUT_NAME}"
  else
    OUT="${SRC_DIR}/out/arupa-${OS}-${cpu}-${VER}-static"
  fi
  [[ -d "${OUT}" ]] || err "没有 out 目录（先跑 scripts/build-arupa-desktop.sh --arch ${cpu}）: ${OUT}"
  OUT_USED="${OUT}"

  DIST="${DIST_DIR}/arupa-${OS}-${cpu}-${VER}-static-${NUM}"
  if [[ -e "${DIST}" ]]; then
    if [[ "${FORCE}" -eq 1 ]]; then
      warn "目标已存在，--force 删除重建: ${DIST}"
      rm -rf "${DIST}"
    else
      err "目标已存在（加 --force 覆盖）: ${DIST}"
    fi
  fi

  # 中途失败别留下空壳 dist 目录（会把下一次的序号顶上去，也会被误当成完整包）
  trap 'st=$?; if [[ ${st} -ne 0 && -d "${DIST}" ]]; then rm -rf "${DIST}"; fi' EXIT

  log "交付包: ${DIST}"
  log "交付 id: ${DELIVERY_ID}  （ver=${VER} n=${NUM} os=${OS} arch=${cpu}）"

  package_arch "${cpu}" "${OUT}" "${DIST}"
  write_sums_and_manifest "${DIST}" "${cpu}"

  if [[ "${DO_ZIP}" -eq 1 ]]; then
    zip_path="${DIST}.zip"
    rm -f "${zip_path}"
    zip_opts=(-q -r)
    # mac 的 macKernel 是软链：-y 存成链接，否则 kernel/ 里的 dylib 会在包里再存一份。
    if [[ "${OS}" == "mac" ]]; then zip_opts+=(-y); fi
    ( cd "${DIST_DIR}" && zip "${zip_opts[@]}" "$(basename "${zip_path}")" "$(basename "${DIST}")" ) \
      || err "打 zip 失败（需要 zip 命令）"
    log "zip: ${zip_path}"
  fi

  trap - EXIT
  echo "    kernel 体积: $(du -sh "${DIST}/kernel" | cut -f1)"
done

echo
echo "[✓] 完成: ${OS} ${VER} n=${NUM}"
for cpu in "${ARCHS[@]}"; do
  echo "    dist/arupa-${OS}-${cpu}-${VER}-static-${NUM}"
done
