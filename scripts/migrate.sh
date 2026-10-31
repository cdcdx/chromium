#!/usr/bin/env bash
# =============================================================================
# scripts/migrate.sh —— 把 dist 下的内核交付包装载进 nomad_android/app
#
# 来源选择（优先级从高到低）：
#   1) -k <目录名|路径>：显式指定 dist 下的目录（也接受绝对路径）
#   2) 自动选择：只在 dist 下的**内核交付目录**里取占用最大的那个（并列取字典序较大）。
#      内核交付目录 = 带 arm64/x64 或 kernel/arm64|kernel/x64 布局的目录。
#      dist 下同时躺着内核交付目录（arupa-android-*-static-*）与 App 安装包目录
#      （nomad-android-*-debug-*，只有 APK、没有 ABI 布局），只按体积挑会误选后者：
#      2026-10-20 实际踩到——自动选中 nomad-android-arm64-...-debug-58 后报
#      "来源目录里没有 arm64/x64"。不读也不写 dist/.delivery-build。
#
# 迁移映射（srcroot = dist/<dir>/kernel 或 dist/<dir>，自动探测）：
#   {abi}/snapshot_blob.bin            -> app/kernel-assets/{abi}/snapshot_blob_64.bin
#   {abi}/arupa-kernel.aar             -> app/libs/kernel/{abi}/arupa-kernel.aar
#   arm64/v8_context_snapshot_64.bin   -> app/kernel-assets/arm64/  （仅 arm64，见下）
#   {abi}/arupa_kernel.pak             -> app/kernel-assets/{abi}/arupa_kernel.pak （按 ABI，非共享）
#   arm64/icudtl.dat                   -> app/src/main/assets/icudtl.dat         （共享件）
#   standard/libbox.aar                -> app/libs/standard/libbox.aar           （可选件）
#
# libbox.aar 不是内核交付的必带件：交付包里有就替换并同步指纹，没有就跳过、不报错
# （复核里按 SKIP 计，不计入不一致）。
# v8 上下文快照只迁 arm64（V8_CTX_ABIS）：x64 的 .so 不引用该文件（只剩
# v8_context_snapshot_impl.cc 源码路径字符串，无文件名查找），工程契约里也没有它
# —— tools/restore-local-files.sh 的 FILES、提交版 runtime-manifest.json、
# app/build.gradle 的 kernelAbiAssetSha256 都只登记 arm64 那份。
# 2026-09-28 之前这里对每个 ABI 都无条件迁移，x64 那份因此被迁进工程并**新增**进
# 清单，使清单与提交版契约分叉。非契约 ABI 上若工程里残留该文件，本脚本会删除它
# 并在清单里移除对应条目（默认不备份；需要回滚件时加 --backup）。
#
# 迁移后自动同步 tools/ci/runtime-manifest.json（Gradle 的 verifyMv3KernelDelivery
# 期望值全读它，不同步会导致 :app:assembleDebug 直接失败）：
#   files[...]              涉及文件的 sha256（不再需要的条目会被移除，见上面 v8 段）
#   kernelResourceApkSha256 AAR 内 assets/arupa_kernel_resources.apk 的 sha256
#   kernelDeliveryId        取交付件的 .arupa-delivery-id / capability-manifest.json
#
# 用法：
#   bash scripts/migrate.sh                       # 自动选择 dist 下最大目录迁移
#   bash scripts/migrate.sh -k kernel-android-154.0.8037.21-6
#   bash scripts/migrate.sh -k 1arupa-android-154.0.8037.21+14-test
#   bash scripts/migrate.sh --list                # 列出 dist 下可迁移目录
#   bash scripts/migrate.sh --dry-run             # 只打印将做什么
#   bash scripts/migrate.sh --abi arm64           # 只迁 arm64（共享件仍从 arm64 取）
#   bash scripts/migrate.sh --no-manifest         # 跳过指纹同步
#   bash scripts/migrate.sh --backup              # 默认不备份；需要回滚件时才显式开启
# =============================================================================
set -uo pipefail

WORKSPACE=${WORKSPACE:-/home/ural/chromium_android}
DIST=${DIST:-$WORKSPACE/dist}
APP=${APP:-$WORKSPACE/nomad_android}
PKG=com.nomadbrowser.app

DIR=""
ABI=all
DRY=0
DO_MANIFEST=1
DO_BACKUP=0        # 默认不备份（不再建 /tmp/migrate-backup-*）；需要回滚件时加 --backup

log()  { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
err()  { printf '[ERROR] %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

# ── 内核交付目录（自动选择与 --list 的唯一实现）───────────────────────────────
# 判定：目录自身或 kernel/ 下带 arm64|x64 布局，才是本脚本能消费的内核交付包。
# 只按体积挑会把 dist 下的 App 安装包目录（如 nomad-android-*-debug-*，只有 APK）选进来。
kernel_dirs() {   # 输出每行 "<字节数> <目录名>"，按 (字节数, 名字) 升序
  DIST="$DIST" python3 - <<'PY'
import os
ABIS = ('arm64', 'x64')
def eligible(p):
    return any(os.path.isdir(os.path.join(p, a)) or
               os.path.isdir(os.path.join(p, 'kernel', a)) for a in ABIS)
rows = []
try:
    with os.scandir(os.environ['DIST']) as it:
        for e in it:
            if not e.is_dir() or e.name.startswith('.'):
                continue
            if not eligible(e.path):
                continue
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _ds, fs in os.walk(e.path) for f in fs)
            rows.append((size, e.name))
except OSError:
    pass
for size, name in sorted(rows):
    print(f"{size} {name}")
PY
}

pick_auto_dir() {   # 占用最大的内核交付目录名（并列取字典序较大）；无可用则输出空
  kernel_dirs | tail -n 1 | cut -d' ' -f2
}

while [ $# -gt 0 ]; do
  case "$1" in
    -k|--dir)     DIR=${2:?-k 需要目录名}; shift ;;
    --abi)        ABI=${2:?--abi 需要参数}; shift ;;
    --dry-run|-n) DRY=1 ;;
    --no-manifest) DO_MANIFEST=0 ;;
    --no-backup)  DO_BACKUP=0 ;;           # 已是默认，保留兼容
    --backup)     DO_BACKUP=1 ;;
    --list|-l)
      echo "dist 下的内核交付目录（可迁移）："
      kernel_dirs | while read -r sz name; do
        printf '    %-46s %6s MB\n' "$name" "$((sz/1000000))"
      done
      exit 0 ;;
    -h|--help)    sed -n '2,34p' "$0"; exit 0 ;;
    *)            die "未识别参数: $1（-h 看用法）" ;;
  esac
  shift
done

[ -d "$DIST" ] || die "dist 不存在: $DIST"
[ -d "$APP" ]  || die "App 仓库不存在: $APP"

# ── 定位来源目录 ────────────────────────────────────────────────────────────
if [ -z "$DIR" ]; then
  # 不再读取 dist/.delivery-build：只在**内核交付目录**里自动选择（见文件头说明）
  DIR=$(pick_auto_dir)
  [ -n "$DIR" ] || die "未指定 -k 且 dist 下无内核交付目录（需带 arm64/x64 或 kernel/<abi> 布局）"
  log "来源（自动选择 dist 下最大内核交付目录）: $DIR"
  AUTO_DIR=$DIR
else
  log "来源（命令行 -k）: $DIR"
fi

case "$DIR" in
  /*) SRC=$DIR ;;                       # 绝对路径直接用
  *)  SRC=$DIST/$DIR ;;
esac
if [ ! -d "$SRC" ]; then
  # 自动选择的目录可能已被外部构建进程替换/移除：改回退到 dist 下最大的现存内核
  # 交付目录（同一套判定，见文件头说明），而不是报错退出。
  DIR=$(pick_auto_dir)
  [ -n "$DIR" ] || die "未指定 -k 且 dist 下无内核交付目录（需带 arm64/x64 或 kernel/<abi> 布局）"
  AUTO_DIR=$DIR
  case "$DIR" in /*) SRC=$DIR;; *) SRC=$DIST/$DIR;; esac
  [ -d "$SRC" ] || die "回退得到的目录也不存在: $SRC"
  log "  已回退到: $DIR"
fi

# 兼容两种布局：dist/<dir>/kernel/{abi} 与 dist/<dir>/{abi}
if [ -d "$SRC/kernel" ]; then SRCROOT=$SRC/kernel; else SRCROOT=$SRC; fi

ABIS=()
case "$ABI" in
  all)   for a in arm64 x64; do [ -d "$SRCROOT/$a" ] && ABIS+=("$a"); done ;;
  arm64|x64) [ -d "$SRCROOT/$ABI" ] || die "来源缺少 $ABI: $SRCROOT/$ABI"; ABIS=("$ABI") ;;
  *)     die "--abi 只支持 all|arm64|x64" ;;
esac
[ ${#ABIS[@]} -gt 0 ] || die "来源目录里没有 arm64/x64: $SRCROOT"
log "源根: ${SRCROOT#$WORKSPACE/}   ABI: ${ABIS[*]}"

# ── 拷贝（默认不备份） ──────────────────────────────────────────────────────
BACKUP=""
if [ "$DO_BACKUP" -eq 1 ] && [ "$DRY" -eq 0 ]; then
  BACKUP=/tmp/migrate-backup-$(date +%Y%m%d-%H%M%S)
  mkdir -p "$BACKUP"
  log "备份目录: $BACKUP"
fi

sha() { python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

COPIED=()          # "目标相对路径|源绝对路径|sha256"
copy() {           # copy <源> <目标> <目标相对路径(报告用)>
  local s=$1 d=$2 rel=$3
  [ -f "$s" ] || { warn "缺源 $s ，跳过"; return 1; }
  if [ "$DRY" -eq 1 ]; then
    # 预演：拷贝是字节复制，目标 sha256 就是源文件的，据此预演指纹变更
    printf '  %-46s <- %s\n' "$rel" "${s#$WORKSPACE/}"
    COPIED+=("$rel|$s|$(sha "$s")")
    return 0
  fi
  mkdir -p "$(dirname "$d")"
  cp -f "$s" "$d.tmp" && mv -f "$d.tmp" "$d" || { err "拷贝失败: $s -> $d"; return 1; }
  local h; h=$(sha "$d")
  local sz=$(( $(stat -c%s "$d") / 1000000 ))
  printf '  %-46s %5s MB  %s\n' "$rel" "$sz" "${h:0:12}"
  COPIED+=("$rel|$s|$h")
  return 0
}

# 交付 v8 上下文快照的 ABI（与 scripts/builder/kernel.py 的 V8_CTX_ABIS 同源）
V8_CTX_ABIS="arm64"
REMOVED=()   # 需要从工程与清单里一并移除的残留（存"目标相对路径"）

# 要不要迁 v8 上下文快照，**由该 ABI 的 .so 决定**（不是按 ABI 硬编码）：
# USE_V8_CONTEXT_SNAPSHOT 开着时 .so 会按文件名找 assets/v8_context_snapshot_64.bin
# （gin/v8_initializer.cc），关掉时二进制里没有该文件名（只剩 .cc 源码路径串）。
# 2026-09-28 事故：x64 被"按 ABI"规则扣掉，而 .so 仍是开关打开的旧构建 → 模拟器渲染
# 进程 [FATAL:gin/v8_initializer.cc:640] Error loading V8 startup snapshot file。
need_ctx_snapshot() {   # need_ctx_snapshot <abi> <so路径>
  local abi=$1 so=$2 name="v8_context_snapshot_64.bin"
  [ "$abi" = "x86" ] && name="v8_context_snapshot_32.bin"
  [ -f "$so" ] && grep -aq -- "$name" "$so"
}

# 找该 ABI 的内核 .so（供 v8 快照需求探测）。两种交付形态：
#   kernel1: kernel/<abi>/libarupa_kernel.so（裸文件）
#   kernel2: .so 只在 AAR 内 jni/<jni-dir>/libarupakernel.so —— 临时解出来探测，用完即删。
# 2026-09-29 事故：kernel2 交付包里没有裸 .so，need_ctx_snapshot 的 [ -f ] 恒假，
# arm64 被误判"不需要"v8 快照，App 侧文件被当残留删除 → 复核 FAIL / Gradle 拒绝构建。
find_kernel_so() {   # find_kernel_so <abi> -> 输出 .so 绝对路径（找不到则空、返回 1）
  local abi=$1 n aar jdir tmp
  for n in libarupa_kernel.so libarupakernel.so; do
    [ -f "$SRCROOT/$abi/$n" ] && { printf '%s\n' "$SRCROOT/$abi/$n"; return 0; }
  done
  aar="$SRCROOT/$abi/arupa-kernel.aar"
  [ -f "$aar" ] || return 1
  case "$abi" in
    arm64) jdir=arm64-v8a ;;
    x64)   jdir=x86_64 ;;
    x86)   jdir=x86 ;;
    *)     jdir="$abi" ;;
  esac
  tmp="$(mktemp -d)/libarupakernel.so"
  if unzip -p "$aar" "jni/$jdir/libarupakernel.so" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    printf '%s\n' "$tmp"; return 0
  fi
  rm -rf "$(dirname "$tmp")"
  return 1
}

# 可选件 libbox.aar（传输引擎，非内核交付必带件）。按候选位置逐个找，找到第一个就返回。
# 2026-10-08：内核交付包（kernel/{abi}）里没有它，App 侧也没有 → 复核报 FAIL、误判"Gradle
# 会拒绝构建"。此处改为"有就替换、没有就跳过"，缺失不再算不一致。
LIBBOX_REL="app/libs/standard/libbox.aar"
OPTIONAL_RELS=("$LIBBOX_REL")   # 复核时允许"清单有条目、文件不存在"
find_libbox_aar() {   # -> 输出源路径（找不到返回 1）
  local c f
  for c in "$SRC/standard/libbox.aar" "$SRCROOT/../standard/libbox.aar" \
           "$SRC/libbox.aar" "$SRCROOT/libbox.aar" "$DIST/standard/libbox.aar"; do
    [ -f "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  f=$(find "$SRC" -maxdepth 3 -type f -name 'libbox.aar' -print -quit 2>/dev/null)
  [ -n "$f" ] && { printf '%s\n' "$f"; return 0; }
  return 1
}

log "迁移文件："
for a in "${ABIS[@]}"; do
  copy "$SRCROOT/$a/snapshot_blob.bin" "$APP/app/kernel-assets/$a/snapshot_blob_64.bin" "app/kernel-assets/$a/snapshot_blob_64.bin"
  copy "$SRCROOT/$a/arupa_kernel.pak" "$APP/app/kernel-assets/$a/arupa_kernel.pak" "app/kernel-assets/$a/arupa_kernel.pak"
  kernel_so=$(find_kernel_so "$a" || true)
  so_tmpdir=""
  if [ -n "$kernel_so" ] && [ "$(dirname "$kernel_so")" != "$SRCROOT/$a" ]; then
    so_tmpdir="$(dirname "$kernel_so")"
  fi
  case "$(need_ctx_snapshot "$a" "$kernel_so" && echo yes)" in
    yes)
      copy "$SRCROOT/$a/v8_context_snapshot_64.bin" "$APP/app/kernel-assets/$a/v8_context_snapshot_64.bin" "app/kernel-assets/$a/v8_context_snapshot_64.bin"
      ;;
    *)
      # 该 ABI 的 .so 不需要 v8 上下文快照（二进制里没有该文件名）。
      [ -f "$SRCROOT/$a/v8_context_snapshot_64.bin" ] && warn "交付包里带 $a/v8_context_snapshot_64.bin（该 ABI 不需要）—— 已忽略；请检查打包端"
      stale="$APP/app/kernel-assets/$a/v8_context_snapshot_64.bin"
      if [ -f "$stale" ]; then
        rel="app/kernel-assets/$a/v8_context_snapshot_64.bin"
        if [ "$DRY" -eq 1 ]; then
          printf '  %-46s 删除（%s 不交付 v8 上下文快照）\n' "$rel" "$a"
        else
          rm -f "$stale" && printf '  %-46s 删除（%s 不交付 v8 上下文快照）\n' "$rel" "$a"
        fi
        REMOVED+=("$rel")
      fi
      ;;
  esac
  if [ -n "$so_tmpdir" ]; then
    rm -rf "$so_tmpdir"
  fi
  copy "$SRCROOT/$a/arupa-kernel.aar" "$APP/app/libs/kernel/$a/arupa-kernel.aar" "app/libs/kernel/$a/arupa-kernel.aar"
  copy "$SRCROOT/$a/.arupa-delivery-id" "$APP/app/libs/kernel/$a/.arupa-delivery-id" "app/libs/kernel/$a/.arupa-delivery-id"
done

# 共享件：优先 arm64，缺失时退回任一可用 ABI
SHARED_ABI=arm64
[ -d "$SRCROOT/arm64" ] || SHARED_ABI=${ABIS[0]}
copy "$SRCROOT/$SHARED_ABI/icudtl.dat"       "$APP/app/src/main/assets/icudtl.dat"       "app/src/main/assets/icudtl.dat"

# 可选件：交付包里有 libbox.aar 就替换并同步指纹；没有就跳过，不报错、不改清单。
# 顺带读同级 .libbox-delivery-id（若有）同步 libboxDeliveryId。
LIBBOX_DELIVERY=""
if libbox_src=$(find_libbox_aar); then
  copy "$libbox_src" "$APP/$LIBBOX_REL" "$LIBBOX_REL" || true
  for idf in "$(dirname "$libbox_src")/.libbox-delivery-id" "$(dirname "$libbox_src")/libbox-delivery-id.txt"; do
    [ -f "$idf" ] && { LIBBOX_DELIVERY=$(head -1 "$idf" | tr -d '\r' | xargs); break; }
  done
else
  log "  可选件 libbox.aar：交付包里没有 —— 跳过（$LIBBOX_REL 保持原状）"
fi

if [ "$DRY" -eq 0 ]; then
  [ ${#COPIED[@]} -gt 0 ] || die "没有任何文件被迁移"
fi

# ── 同步 runtime-manifest.json ──────────────────────────────────────────────
if [ "$DO_MANIFEST" -eq 1 ]; then
  MF=$APP/tools/ci/runtime-manifest.json
  [ -f "$MF" ] || { warn "无 $MF，跳过指纹同步"; DO_MANIFEST=0; }
fi

if [ "$DO_MANIFEST" -eq 1 ]; then
  # 交付号：.arupa-delivery-id 优先，其次 capability-manifest.json
  DELIVERY=""
  for a in "${ABIS[@]}"; do
    [ -f "$SRCROOT/$a/.arupa-delivery-id" ] && { DELIVERY=$(head -1 "$SRCROOT/$a/.arupa-delivery-id" | tr -d '\r' | xargs); break; }
  done
  if [ -z "$DELIVERY" ]; then
    for f in "$SRC/capability-manifest.json" "$SRCROOT/capability-manifest.json"; do
      [ -f "$f" ] && { DELIVERY=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('delivery_id',''))" "$f" 2>/dev/null); break; }
    done
  fi

  export MF APP DELIVERY SRC SRCROOT DRY LIBBOX_DELIVERY
  export ABIS_STR=$(printf '%s\n' "${ABIS[@]}")
  export COPIED_STR=$(printf '%s\n' "${COPIED[@]}")
  # 需要从清单里移除的残留条目（空数组也安全，见 V8_CTX_ABIS 段）
  export REMOVED_STR=$(printf '%s\n' ${REMOVED[@]+"${REMOVED[@]}"})
  python3 - <<'PY' || die "同步 tools/ci/runtime-manifest.json 失败"
import hashlib, json, os, sys, zipfile, pathlib
mf = pathlib.Path(os.environ['MF'])
m = json.loads(mf.read_text(encoding='utf-8'))
files = m.setdefault('files', {})
changed = []
# 每行一条 "相对路径|源绝对路径|sha256"
for line in os.environ['COPIED_STR'].splitlines():
    if not line.strip():
        continue
    rel, _src, sha = line.split('|')[:3]
    if rel in files:
        if files[rel] != sha:
            changed.append(f"  files[{rel}]\n    - {files[rel][:16]}\n    + {sha[:16]}")
            files[rel] = sha
    else:
        changed.append(f"  files[{rel}] 新增 {sha[:16]}")
        files[rel] = sha

# AAR 内 resources.apk（Gradle 单独校验这个字段）
DRY = os.environ.get('DRY') == '1'
SRCROOT = pathlib.Path(os.environ.get('SRCROOT', ''))
APP = str(mf.parent.parent.parent)
for abi in ('arm64', 'x64'):
    # dry-run 时 App 里还是旧包，必须按来源 AAR 预演
    aar = (SRCROOT / abi / 'arupa-kernel.aar') if DRY else (pathlib.Path(APP) / f'app/libs/kernel/{abi}/arupa-kernel.aar')
    if not aar.is_file():
        continue
    try:
        with zipfile.ZipFile(aar) as z:
            res = hashlib.sha256(z.read('assets/arupa_kernel_resources.apk')).hexdigest()
    except KeyError:
        continue
    old = m.get('kernelResourceApkSha256')
    if old != res:
        changed.append(f"  kernelResourceApkSha256（取自 {abi}）\n    - {(old or '空')[:16]}\n    + {res[:16]}")
        m['kernelResourceApkSha256'] = res
    break

# 能力事实（声明 vs 实现）：内核出包时已由 capability-manifest.json 从**源码**派生
# （scripts/builder/kernel.py::verify_capability_claims 门禁过的结果）。这里把它压成
# 宿主能 1:1 对照运行期 ArupaMv3Guard.selfTest() 事实行的几个字段。
# 没有这段的后果是"清单说什么都行"——设备用例会因此拒绝：交付件与清单必须两边同时
# 带/不带（见 ArupaKernelDeliveryDeviceTest.deliveredCapabilityFactsMatchRuntimeGuardFacts）。
CAP_REQUIRED = ('events', 'aliases', 'worker_window', 'wake_outbox')
cap_src = None
for base in (os.environ.get('SRC', ''), os.environ.get('SRCROOT', '')):
    if not base:
        continue                       # 空变量会退化成 CWD，别把当前目录的清单当交付件
    cand = pathlib.Path(base) / 'capability-manifest.json'
    if cand.is_file():
        cap_src = cand
        break
if cap_src is None:
    changed.append("  kernelCapabilityFacts 跳过（交付包没有 capability-manifest.json："
                   "老交付件或未走 kernel package）")
    if m.pop('kernelCapabilityFacts', None) is not None:
        changed.append("  移除 kernelCapabilityFacts（新交付件不带能力清单）")
else:
    cm = json.loads(cap_src.read_text(encoding='utf-8'))
    if not all(k in cm for k in CAP_REQUIRED):
        changed.append("  kernelCapabilityFacts 跳过（"
                       + cap_src.name + " 是老 schema，不含 " + '/'.join(CAP_REQUIRED) + "）")
        if m.pop('kernelCapabilityFacts', None) is not None:
            changed.append("  移除 kernelCapabilityFacts（新交付件的能力清单为老 schema）")
    else:
        codes = list((cm.get('wake_outbox', {}).get('error_codes') or {}).values())
        ww = cm.get('worker_window', {})
        facts = {
            'gate': (cm.get('declaration_vs_implementation') or {}).get('gate', ''),
            'alias': '+'.join(cm['aliases'].get('namespaces') or []),
            'capability': cm['aliases'].get('capability', ''),
            'hostFireableEvents': len(cm['events'].get('host_fireable') or []),
            'netdiskGatedEvents': len(cm['events'].get('netdisk_permission_gated') or []),
            # 顺序与 ArupaMv3SelfTest 事实行一致：最长租约/业务/提交预留
            'windowMs': '/'.join(str(ww.get(k, '')) for k in
                                 ('max_lease_ms', 'business_budget_max_ms', 'commit_reserve_ms')),
            'maxItems': ww.get('max_items'),
            'wakeCodes': (f"{codes[0]}..{codes[-1]}" if codes else ''),
            # 与运行期 nomadTransferCapabilities().window **逐键同名**（内核清单已保证），
            # 这里原样搬运、不做映射 —— 映射表就是下一个漂移入口。
            'window': ww.get('window_facts') or {},
            # 宿主可投递的网盘事件名（运行期 supportedEvents 应与之逐项一致）
            'netdiskEvents': cm['events'].get('netdisk_permission_gated') or [],
        }
        if m.get('kernelCapabilityFacts') != facts:
            changed.append("  kernelCapabilityFacts（取自 " + cap_src.name + "）\n    - "
                           + json.dumps(m.get('kernelCapabilityFacts'), ensure_ascii=False)
                           + "\n    + " + json.dumps(facts, ensure_ascii=False))
            m['kernelCapabilityFacts'] = facts

# 移除不该存在于契约里的条目（例如 x64 的 v8 上下文快照）
for rel in (os.environ.get('REMOVED_STR') or '').splitlines():
    if rel.strip() and rel in files:
        changed.append(f"  files[{rel}] 移除（该 ABI 不交付 v8 上下文快照）")
        files.pop(rel)

did = os.environ.get('DELIVERY', '')
if did and m.get('kernelDeliveryId') != did:
    changed.append(f"  kernelDeliveryId\n    - {m.get('kernelDeliveryId')}\n    + {did}")
    m['kernelDeliveryId'] = did

# 逐 ABI 交付号：App 的 BuildConfig.KERNEL_VERSION 与设备用例的 expectedVersion()
# 读的都是 kernelDeliveryIds[abi]（app/build.gradle）。只更新上面那个单值，会出现
# "App 报的版本还是上一份交付件"—— 又一例声明与实现错开（N-C08 家族）。
ids = m.setdefault('kernelDeliveryIds', {})
for abi in (os.environ.get('ABIS_STR') or '').splitlines():
    abi = abi.strip()
    if not abi or not did or ids.get(abi) == did:
        continue
    changed.append(f"  kernelDeliveryIds[{abi}]\n    - {ids.get(abi)}\n    + {did}")
    ids[abi] = did

lid = os.environ.get('LIBBOX_DELIVERY', '')
if lid and m.get('libboxDeliveryId') != lid:
    changed.append(f"  libboxDeliveryId\n    - {m.get('libboxDeliveryId')}\n    + {lid}")
    m['libboxDeliveryId'] = lid

# 能力清单副本：:app:verifyMv3KernelDelivery 先校验副本 sha256，再比对其
# delivery_id / locked_sha256 与本次迁移的 AAR。只同步上面的 kernelCapabilityFacts
# 而不同步副本，内核一升版就会在出包时被拦下：
#   "内核能力清单与 <abi> 构建版本或 AAR 指纹不一致"（2026-10-22 +62→+63 实际踩到）。
if cap_src is not None:
    cm_did = cm.get('delivery_id')
    if did and cm_did and cm_did != did:
        print(f"[ERROR] 交付件自相矛盾：{cap_src.name} delivery_id={cm_did}，"
              f"而 .arupa-delivery-id={did}；请用同一份出包产物重迁。", file=sys.stderr)
        raise SystemExit(1)
    rel = m.get('kernelCapabilityManifest') or 'tools/ci/kernel-capability-manifest.json'
    dst = pathlib.Path(rel)
    if not dst.is_absolute():
        dst = pathlib.Path(APP) / rel
    cap_bytes = cap_src.read_bytes()
    cap_sha = hashlib.sha256(cap_bytes).hexdigest()
    cap_same = dst.is_file() and dst.read_bytes() == cap_bytes
    if not cap_same:
        old_sha = hashlib.sha256(dst.read_bytes()).hexdigest()[:16] if dst.is_file() else '缺失'
        changed.append(f"  能力清单副本 {rel}\n    - {old_sha}\n    + {cap_sha[:16]}")
        if not DRY:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(cap_bytes)
    if m.get('kernelCapabilityManifestSha256') != cap_sha:
        changed.append("  kernelCapabilityManifestSha256\n    - "
                       + str(m.get('kernelCapabilityManifestSha256'))[:16]
                       + "\n    + " + cap_sha[:16])
        m['kernelCapabilityManifestSha256'] = cap_sha

DRY = os.environ.get('DRY') == '1'
if DRY:
    print("  [预演] 将写入 runtime-manifest.json：")
else:
    mf.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')
    print("  指纹同步：")
print("\n".join(changed) if changed else "  （无变化）")
PY
fi

# ── 复核 ────────────────────────────────────────────────────────────────────
if [ "$DRY" -eq 1 ]; then
  log "--dry-run，未做任何改动（上面是指纹的预演结果）"
  exit 0
fi
echo
log "复核（清单 vs 实际文件）："
export APP
export OPTIONAL_STR=$(printf '%s\n' ${OPTIONAL_RELS[@]+"${OPTIONAL_RELS[@]}"})
python3 - <<'PY'
import hashlib, json, os, pathlib, zipfile
APP = pathlib.Path(os.environ.get('APP', '/home/ural/chromium_android/nomad_android'))
mf = APP / 'tools/ci/runtime-manifest.json'
if not mf.is_file():
    print("  无清单，跳过")
    raise SystemExit
m = json.loads(mf.read_text(encoding='utf-8'))
# 可选件（如 libbox.aar）：交付包没带就不算不一致，只提示
optional = set(filter(None, (os.environ.get('OPTIONAL_STR') or '').splitlines()))
bad = 0
for rel, want in m.get('files', {}).items():
    p = APP / rel
    got = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    if got == want:
        print(f"  OK   {rel}")
    elif got is None and rel in optional:
        print(f"  SKIP {rel}（可选件未交付，不计入不一致）")
    else:
        bad += 1
        print(f"  FAIL {rel}" + ("" if got else "（文件不存在）"))
print(f"  kernelDeliveryId = {m.get('kernelDeliveryId')}")
print(f"  kernelResourceApkSha256 = {(m.get('kernelResourceApkSha256') or '')[:16]}")
print("  结论:", "全部一致" if bad == 0 else f"{bad} 项不一致（Gradle 会拒绝构建）")
PY

echo
log "完成。下一步："
log "  cd $WORKSPACE && bash build.sh android build --os android [--arch arm64]"
[ -n "$BACKUP" ] && log "  回滚：把 $BACKUP 下同名文件拷回 $APP/app/..."
