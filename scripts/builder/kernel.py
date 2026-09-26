#!/usr/bin/env python3
# =============================================================================
#  nomad 内核编译 / 测试 / 打包
#
#  编译目录: out/kernel-<os>-<arch>-<ver>-<static|dynamic>
#  交付目录: dist/kernel-<os>-<arch>-<ver>-<n>   +  同名 .zip / .gate.json
#
#  关键约束（都是拿事故换来的，别删）:
#   · 编进产物的是挂载点那份代码 —— src/chrome/browser/arupa_desktop 必须指向
#     nomadbrowser.kernel/，挂载错 = 编错树。
#   · dynamic（组件构建）产物离开构建树就不可加载 —— 只有 static 允许出交付包。
#   · 交付包编号只在"校验通过"之后才消耗，避免一次误跑白跳一个号。
# =============================================================================
from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from .common import (SRC, KERNEL_REPO, PC_REPO, DIST_ROOT, TOOLS_DIR, Ctx, build_cmd, cget,
                     err, gn_gen, git, log, out, probe_steps, purge_previous_deliveries,
                     run, warn, write_args_gn, write_sha256sums, zip_dir, extra_gn_args,
                     ensure_depot_tools, next_delivery_no, record_delivery, human_size,
                     ensure_android_native_deps)
from . import common

MODULE_RELDIR = "chrome/browser/arupa_desktop"
KERNEL_TARGET = f"{MODULE_RELDIR}:arupa_kernel"
RENDER_TARGET = f"{MODULE_RELDIR}:render"
# Android 交付件需要 v8_context_snapshot_64.bin（v8 上下文快照）。
# 注意：ninja 目标名不带 gn 的前导 "//"（build.ninja 里写作
# tools/v8_context_snapshot$:generate_v8_context_snapshot）
V8_SNAPSHOT_TARGET = "tools/v8_context_snapshot:generate_v8_context_snapshot"

# 测试可执行目标（全部 testonly，BUILD.gn 定义；按图里是否存在的实际过滤）
TEST_TARGETS = [
    "arupa_mv3perm", "arupa_routeguard", "arupa_capi_extroot_test", "arupa_extdispatch_test",
    "arupa_cookies_test", "arupa_action_test", "arupa_extsurface_test", "arupa_swvariants_test",
    "arupa_routefirst_test", "arupa_i18n_test", "arupa_navfail_test", "arupa_extlist_test",
    "arupa_swlifecycle_test", "arupa_schema_test", "arupa_mv3guard", "arupa_injectguard",
    "arupa_mv3isolate", "arupa_jsbridge", "arupa_leak_test", "arupa_cdpdetect", "arupa_g7_test",
]

V8_ARCH = {"arm64": "arm64", "x64": "x86_64", "x86": "x86_32"}

# 交付件清单：required 缺一即终止；optional 缺了只告警
ARTIFACTS = {
    "mac": {
        "required": ["libarupa_kernel.dylib", "arupa_render"],
        "optional": ["icudtl.dat", "snapshot_blob.bin", "v8_context_snapshot.{v8}.bin",
                     "content_shell.pak", "devtools_resources.pak", "shell_resources.pak",
                     "libEGL.dylib", "libGLESv2.dylib", "libvk_swiftshader.dylib",
                     "libvulkan.dylib", "vk_swiftshader_icd.json",
                     "angledata/VkICD_mock_icd.json", "angledata/VkLayer_khronos_validation.json",
                     "hyphen-data/manifest.json", "resources/inspector_overlay/main.js",
                     "resources/inspector_overlay/inspector_overlay_resources.grd",
                     "Libraries/libtest_trace_processor.dylib"],
    },
    "win": {
        "required": ["arupa_kernel.dll", "arupa_render.exe"],
        "optional": ["icudtl.dat", "snapshot_blob.bin", "v8_context_snapshot.{v8}.bin",
                     "content_shell.pak", "devtools_resources.pak", "shell_resources.pak",
                     "libEGL.dll", "libGLESv2.dll", "vk_swiftshader.dll",
                     "vk_swiftshader_icd.json"],
    },
    "linux": {
        "required": ["libarupa_kernel.so", "arupa_render"],
        "optional": ["icudtl.dat", "snapshot_blob.bin", "v8_context_snapshot.{v8}.bin",
                     "content_shell.pak", "devtools_resources.pak", "shell_resources.pak",
                     "libEGL.so", "libGLESv2.so", "libvk_swiftshader.so",
                     "vk_swiftshader_icd.json"],
    },
    "android": {
        "required": ["libarupa_kernel.so"],
        "optional": ["icudtl.dat", "snapshot_blob.bin", "v8_context_snapshot.{v8}.bin",
                     "content_shell.pak", "devtools_resources.pak", "shell_resources.pak"],
    },
}

# AAR 内 jni 目录名（Android ABI 名 ≠ gn 的 target_cpu 名）
AAR_JNI_DIR = {"arm64": "arm64-v8a", "x64": "x86_64", "x86": "x86"}


def v8_snapshot_name(c: Ctx) -> str:
    """v8 上下文快照文件名 —— 各平台命名规则不同：
    Android 用 _64/_32 后缀（tools/v8_context_snapshot/v8_context_snapshot.gni），
    macOS 用 .<arch>.bin（与 x64 并存），其余平台就是 .bin。"""
    if c.os == "android":
        return "v8_context_snapshot_32.bin" if c.arch == "x86" else "v8_context_snapshot_64.bin"
    if c.os == "mac":
        return f"v8_context_snapshot.{V8_ARCH.get(c.arch, c.arch)}.bin"
    return "v8_context_snapshot.bin"


# ── 前置检查 ────────────────────────────────────────────────────────────────
def preflight(c: Ctx):
    """挂载点必须指向工作区内核仓 —— 否则会出现「改了 A 树、编了 B 树」。"""
    if not SRC.is_dir():
        err(f"Chromium 源码树不存在: {SRC}")
    if not KERNEL_REPO.is_dir():
        err(f"内核仓不存在: {KERNEL_REPO}")
    if c.no_link:
        warn("--no-link: 跳过挂载点检查（编的是挂载点当前那份）")
        return
    import fetch                      # scripts/fetch.py：挂载逻辑只有一份实现
    fetch.do_link()
    mount = SRC / MODULE_RELDIR
    real = Path(os.path.realpath(mount))
    if real != KERNEL_REPO.resolve():
        err(f"挂载点不是工作区内核仓（会「改了 A 树、编了 B 树」）:\n"
            f"  挂载点: {mount} -> {real}\n  期望  : {KERNEL_REPO}\n"
            f"  修: rm -f '{mount}' && ln -s '{KERNEL_REPO}' '{mount}'\n"
            f"  或确知要编挂载点那份: --no-link")


GN_ALL_MARKER = f'"//{MODULE_RELDIR}:arupa_kernel"'


def patch_gn_all():
    """把内核目标挂进根 BUILD.gn 的 gn_all —— gn 只加载可达的 BUILD.gn，
    挂不上就是"构建图里没这个目标"。
    必须唯一：源码树可能同时挂着别的内核挂载点（arupa_bigbang 等），它们也有
    shared_library("arupa_kernel") 且输出同名 dylib，一起挂进 gn_all 会让 gn
    因"多个目标产出同一文件"直接失败。故这里不是追加，而是保证只留本项目那条。"""
    root = SRC / "BUILD.gn"
    if not root.exists():
        err(f"缺 {root}（源码树不完整？）")
    if common.DRY_RUN:                      # 改源码树的操作，dry-run 下只看不做
        log(f"(dry-run) 检查根 BUILD.gn 的 gn_all 是否挂了 {GN_ALL_MARKER}")
        return
    src = root.read_text(encoding="utf-8", errors="replace")
    other = re.compile(rf'^[ \t]*"//{MODULE_RELDIR.rsplit("/", 1)[0]}/(?!arupa_desktop:)[^"]*:arupa_kernel",[ \t]*\n', re.M)
    removed = other.findall(src)
    src = other.sub("", src)
    if GN_ALL_MARKER in src:
        if removed:
            root.write_text(src, encoding="utf-8")
            log("根 BUILD.gn: 已摘掉别的内核挂载点条目，只留本项目")
        else:
            log(f"根 BUILD.gn 已挂本项目内核目标: {GN_ALL_MARKER}")
        return
    pat = re.compile(r'(group\("gn_all"\) \{\n  testonly = true\n\n  if \(is_cronet_build\) \{\n'
                     r'.*?\n  \} else \{\n    deps = \[\n)', re.S)
    m = pat.search(src)
    if not m:
        err("根 BUILD.gn 里找不到 gn_all 的 deps 块（Chromium 结构变了？）—— 需手动加: "
            f'{GN_ALL_MARKER}')
    src = src[:m.end()] + "      " + GN_ALL_MARKER + ",\n" + src[m.end():]
    root.write_text(src, encoding="utf-8")
    log(f"已向根 BUILD.gn 的 gn_all 追加: {GN_ALL_MARKER}"
        + ("（并摘掉别的内核挂载点条目）" if removed else ""))


def ensure_build_graph(c: Ctx):
    """构建图里没有本项目内核目标就补 patch + gen —— 否则 ninja: unknown target。"""
    if target_in_graph(c, KERNEL_TARGET):
        log("构建图已含本项目内核目标")
        return
    warn("构建图缺本项目内核目标（换过挂载点/别人 gen 过/目录刚建）—— 补 patch + gn gen")
    patch_gn_all()
    gn_gen(c, c.out_dir / "args.gn")


def do_down(c: Ctx):
    ensure_depot_tools(c.cfg)
    if c.os == "android":
        # 从 build 侧直接 down 时 .gclient 常常还没加 target_os=android —— 不加的话
        # gclient sync 不会拉 NDK/SDK/JDK，后面全是"找不到 android 工具链"的噪音。
        import fetch
        fetch.DRY_RUN = common.DRY_RUN      # 两边各有一份 DRY_RUN，不同步就真写了
        fetch.ensure_gclient_target_android()
    from .common import gclient_bin
    cmd = [gclient_bin(), "sync", "--nohooks"]
    if c.os == "android":
        cmd += ["--no-history", "--shallow"]
    cmd += ["--jobs", str(c.jobs or 8)]
    run(cmd, cwd=SRC.parent)
    run([gclient_bin(), "runhooks"], cwd=SRC.parent)


# ── args.gn ─────────────────────────────────────────────────────────────────
def do_gen(c: Ctx):
    preflight(c)
    patch_gn_all()
    ensure_depot_tools(c.cfg)
    if c.os == "android":
        ensure_android_native_deps()
    comp = "false" if c.link == "static" else "true"
    dcheck = "false" if c.link == "static" else "true"
    args = [("use_siso", "false"),
            ("is_debug", "false"),
            ("symbol_level", "0"),
            ("is_component_build", comp),
            ("dcheck_always_on", dcheck),
            ("proprietary_codecs", "true"),
            ("ffmpeg_branding", '"Chrome"'),
            ("target_cpu", f'"{c.arch}"')]
    if c.os == "win":
        args += [("enable_nacl", "false")]
    if c.os == "android":
        # 内核用 //extensions/*（MV3），而 Android 上 enable_extensions 默认假、
        # //extensions/renderer 会 assert(enable_extensions_core) 失败；只有
        # desktop-android 变体才开 enable_desktop_android_extensions ⇒
        # enable_extensions_core（现有官方 AAR 的 .so 含 chrome-extension://，是同一路线）
        args += [("target_os", '"android"'), ("is_desktop_android", "true")]
        # v8 上下文快照（v8_context_snapshot_64.bin，交付件必备）：
        # use_v8_context_snapshot 的默认值显式排除 is_android，故 Android 上默认关；
        # 且 target_os=android 且 v8_current_cpu != v8_target_cpu 时，它会被
        # use_v8_context_snapshot_android_secondary_abi 二次覆盖，必须一并开启。
        args += [("use_v8_context_snapshot", "true"),
                 ("use_v8_context_snapshot_android_secondary_abi", "true")]
    write_args_gn(c.out_dir / "args.gn",
                  f"# nomad kernel — {c.os} {c.arch} {c.link}", args, extra_gn_args(c))
    gn_gen(c, c.out_dir / "args.gn")
    log(f"gn gen 完成: {c.out_dir}")


def target_in_graph(c: Ctx, target: str) -> bool:
    """ninja 目标名里冒号写作 $: —— 按图判定，避免内核仓换版后 unknown target。"""
    f = c.out_dir / "build.ninja"
    if not f.exists():
        return False
    ninja_name = target.replace(":", "$:")
    return ninja_name in f.read_text(encoding="utf-8", errors="replace")


def resolve_targets(c: Ctx):
    targets = [KERNEL_TARGET]
    if target_in_graph(c, RENDER_TARGET):
        targets.append(RENDER_TARGET)
    else:
        warn(f"构建图里没有 {RENDER_TARGET}（子进程薄壳）—— 只编内核本体，"
             f"打包时若缺 render 会直接拦下")
    if c.os == "android":
        # 交付件需要 v8_context_snapshot_64.bin；开关未开时该目标不在图里
        if target_in_graph(c, V8_SNAPSHOT_TARGET):
            targets.append(V8_SNAPSHOT_TARGET)
        else:
            warn(f"构建图里没有 {V8_SNAPSHOT_TARGET}（use_v8_context_snapshot 未生效）"
                 f" —— 交付件将缺少 v8 上下文快照")
    return targets


# ── 编译 ────────────────────────────────────────────────────────────────────
def do_build(c: Ctx):
    preflight(c)
    ensure_depot_tools(c.cfg)
    if c.os == "android":
        ensure_android_native_deps()
    if not (c.out_dir / "build.ninja").exists():
        do_gen(c)
    else:
        ensure_build_graph(c)
    targets = resolve_targets(c)
    steps = probe_steps(c, targets)
    if steps == 0:
        log("所有目标均无工作（ninja: no work to do）—— 跳过编译")
    else:
        if steps and steps >= 5000:
            warn(f"本次要编 {steps} 步 —— 不像一次小改动，多半是级联（sysroot/args.gn/版本变了），"
                 f"先确认再等几小时")
        log(f"编译目标: {' '.join(targets)}")
        if not run(build_cmd(c, targets), cwd=SRC):
            err("编译失败")

    required = [r.format(v8=V8_ARCH.get(c.arch, c.arch)) for r in ARTIFACTS[c.os]["required"]]
    missing = [f for f in required if not (c.out_dir / f).exists()]
    for f in required:
        p = c.out_dir / f
        log(f"产物: {p.name} ({human_size(p)})" if p.exists() else f"缺失: {f}")
    if missing:
        err("核心交付件缺失: " + ", ".join(missing))

    if c.os == "mac" and c.link == "static":
        dylib = c.out_dir / "libarupa_kernel.dylib"
        if dylib.exists():
            log("static 交付: strip -x + codesign -s -（剔除本地符号）")
            run(["strip", "-x", str(dylib)], check=False)
            run(["codesign", "-s", "-", "--force", str(dylib)], check=False)
            log(f"strip 后: {human_size(dylib)}")


# ── 测试 ────────────────────────────────────────────────────────────────────
def do_test(c: Ctx):
    present = [f"{MODULE_RELDIR}:{t}" for t in TEST_TARGETS if target_in_graph(c, f"{MODULE_RELDIR}:{t}")]
    if not present:
        warn("构建图里没有测试目标（gn gen 过后才看得到），跳过")
        return
    log(f"编译 {len(present)} 个测试目标")
    if not run(build_cmd(c, present), cwd=SRC):
        err("测试目标编译失败")
    runner = TOOLS_DIR / "run_native_tests.py"
    if not runner.exists():
        warn(f"缺少 {runner}（原在 scripts/tools/，现备份于 backup/tools/）—— 只编译不运行")
        return
    run(["python3", str(runner), "--binary-dir", str(c.out_dir), *present],
        cwd=SRC, check=False)


# ── 打包 ────────────────────────────────────────────────────────────────────
def newest_source_mtime(repo: Path) -> float:
    files = out(["git", "ls-files"], cwd=repo).splitlines()
    mt = 0.0
    for f in files:
        p = repo / f
        if p.exists():
            mt = max(mt, p.stat().st_mtime)
    return mt


def verify_freshness(c: Ctx, required: list):
    """产物必须由当前源码编出来 —— 挡住"旧 dylib 换新交付号"。"""
    art = [c.out_dir / f for f in required]
    art = [p for p in art if p.exists()]
    if not art:
        if common.DRY_RUN:
            warn("(dry-run) 构建目录里还没有产物 —— 跳过新鲜度校验")
            return
        err("构建目录里没有产物，先 build: python3 scripts/build.py kernel build")
    src_mt = newest_source_mtime(KERNEL_REPO)
    oldest = min(p.stat().st_mtime for p in art)
    if oldest < src_mt:
        msg = (f"产物比源码旧 —— 拒绝打成新交付号（编完又改了源码？）:\n"
               f"  内核源码最新: {time.strftime('%F %T', time.localtime(src_mt))}\n"
               f"  最老产物    : {time.strftime('%F %T', time.localtime(oldest))}\n"
               f"  修: 重新 python3 scripts/build.py kernel build 再打包")
        if c.gate:
            err(msg)
        warn("门禁已跳过（--no-gate）:\n  " + msg)
    else:
        log(f"产物新鲜度校验通过（产物 >= 源码 {time.strftime('%F %T', time.localtime(src_mt))}）")


def copy_assets(c: Ctx, stage: Path, kernel_dir: Path):
    """平台无关件取内核仓 package/ + public/（与编进 dylib 的那份同源）。"""
    pkg = KERNEL_REPO / "package"
    for sub, dst in (("docs", stage / "docs"), ("dotnet", stage / "dotnet"),
                     ("plugin-runtime", kernel_dir / "plugin-runtime")):
        src = pkg / sub
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            log(f"  收录 {sub}/")
        else:
            warn(f"内核仓缺 {src}（平台无关件），跳过")
    capi = KERNEL_REPO / "public" / "arupa_kernel_capi.h"
    if capi.exists():
        (stage / "include").mkdir(parents=True, exist_ok=True)
        shutil.copy2(capi, stage / "include" / capi.name)
        log("  收录 include/arupa_kernel_capi.h")
    else:
        warn(f"缺 C ABI 头: {capi}")
    if c.os == "android":
        probe = pkg / "android" / "probe-plugin"
        if probe.is_dir():
            shutil.copytree(probe, stage / "probe-plugin", dirs_exist_ok=True)
            log("  收录 probe-plugin/")
        else:
            warn(f"内核仓缺 {probe}（Android 探针插件），跳过")
    if c.os == "mac":
        alias = stage / "macKernel"
        if not alias.exists():
            alias.symlink_to("kernel", target_is_directory=True)   # PC 侧按 macKernel 取件
            log("  软链 macKernel -> kernel（PC 侧按此名取内核）")


# ── Android 交付件：AAR 组装 ──────────────────────────────────────────────────
#
# 工作区里既无源码也无 gn 目标、因而无法自编的四件上游私有件：
#   classes.jar（co.arupa.kernel.* Java 层）/ arupa_kernel_resources.apk /
#   arupa_kernel.pak / jni/<abi>/libarupapluginhost.so
# 故 Android 交付件以「同版本参考交付件」的框架层为骨架，只把
# jni/<abi>/libarupakernel.so 换成自编产物 —— 结构与上游平替，内核是自编的。

SKELETON_GLOB = "arupa-android-*"      # 参考交付件（上游官方 android 交付包）


def find_reference_delivery(c: Ctx) -> Path | None:
    """找参考交付件 —— 只取框架层，不取它的内核 .so。"""
    if not DIST_ROOT.is_dir():
        return None
    cands = [p for p in sorted(DIST_ROOT.glob(SKELETON_GLOB))
             if (p / "kernel" / c.arch / "arupa-kernel.aar").is_file()]
    return cands[-1] if cands else None


def aar_replace_jni(skeleton: Path, so: Path, jni_entry: str, out: Path) -> None:
    """以骨架 AAR 为底替换 jni 条目：其余条目（classes.jar /
    arupa_kernel_resources.apk / libarupapluginhost.so）原样保留，
    compress_type 与时间戳一并照搬，避免改写 AAR 结构。"""
    with zipfile.ZipFile(skeleton) as zin, zipfile.ZipFile(out, "w") as zout:
        hit = False
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == jni_entry:
                data = so.read_bytes()
                hit = True
            zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            zi.compress_type = item.compress_type
            zi.external_attr = item.external_attr
            zout.writestr(zi, data)
    if not hit:
        err(f"骨架 AAR 内没有 {jni_entry}: {skeleton}")


def stage_android_delivery(c: Ctx, kernel_dir: Path) -> Path:
    """Android 平台专属补齐：组装 AAR + 带入无法自编的框架层件。"""
    so = kernel_dir / "libarupa_kernel.so"
    if not so.is_file():
        err(f"缺自编内核 .so: {so}")
    ref = find_reference_delivery(c)
    if ref is None:
        err(f"找不到参考交付件（dist/{SKELETON_GLOB}/kernel/{c.arch}/arupa-kernel.aar）——"
            f" 框架层无法自编，必须有一份同版本官方交付件作骨架")
    log(f"参考交付件（仅取其框架层）: {ref.name}")

    jni_dir = AAR_JNI_DIR.get(c.arch, c.arch)
    aar_replace_jni(ref / "kernel" / c.arch / "arupa-kernel.aar", so,
                    f"jni/{jni_dir}/libarupakernel.so", kernel_dir / "arupa-kernel.aar")
    log(f"  组装: arupa-kernel.aar（jni/{jni_dir}/libarupakernel.so <- 自编 {human_size(so)}，"
        f"框架层沿用 {ref.name}）")

    for name in ("arupa_kernel.pak", "arupa_kernel_resources.apk"):
        src = ref / "kernel" / c.arch / name
        if src.is_file():
            shutil.copy2(src, kernel_dir / name)
            log(f"  随骨架带入: {name} ({human_size(src)})")
        else:
            warn(f"参考交付件缺 {name}，跳过")
    return ref


def write_capability_manifest(c: Ctx, stage: Path, n: int, ref: Path) -> None:
    """能力清单 —— 与上游 capability-manifest.json 同 schema，locked_sha256 填本次产物。"""
    import hashlib

    def sha(p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    locked = {p.relative_to(stage / "kernel").as_posix(): sha(p)
              for p in sorted((stage / "kernel").rglob("*")) if p.is_file()}
    abi = {"major": 1, "minor": 24}
    ref_cap = ref / "capability-manifest.json"
    if ref_cap.is_file():                      # ABI 版本随上游，不自创
        try:
            abi = json.loads(ref_cap.read_text(encoding="utf-8")).get("abi", abi)
        except json.JSONDecodeError:
            warn(f"参考交付件的 {ref_cap.name} 解析失败，ABI 版本用默认值")
    data = {
        "schema": "arupa.capability-manifest/1",
        "generated": time.strftime("%Y-%m-%d"),
        "platform": "android",
        "kernel_version": c.ver,
        "delivery_id": f"{c.ver}+{n}",
        "abi": abi,
        "locked_sha256": locked,
    }
    (stage / "capability-manifest.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"  生成: capability-manifest.json（锁定 {len(locked)} 件）")


def write_delivery_markers(c: Ctx, kernel_dir: Path, n: int):
    """宿主硬校验项 —— 缺了 PC 编译第一步就炸：
      kernel/.arupa-version       PC 的内核版本单一真值（UA/UA-CH/引擎三者必须同源，
                                   Directory.Build.targets 取它为版本号，不许手抄）
      kernel/.arupa-delivery-id   Mac/ArupaDelivery.props 用它证明 wrapper(dotnet/)
                                   与 macKernel(kernel/) 来自同一完整交付包
    （macKernel/ 是 kernel/ 的软链，写在 kernel/ 下即等于写在 macKernel/ 下）"""
    (kernel_dir / ".arupa-delivery-id").write_text(f"{c.ver}+{n}\n", encoding="utf-8")
    (kernel_dir / ".arupa-version").write_text(f"{c.ver}\n", encoding="utf-8")
    log(f"  标记: .arupa-delivery-id={c.ver}+{n}   .arupa-version={c.ver}")


def verify_pc_requirements(c: Ctx, stage: Path):
    """按 PC 仓的必需件清单逐件自查 —— PC 编译第一步就会因为缺件报错，
    与其让它把包编到一半再报，不如出包时就拦下。清单直接从 props 读，不复制副本。"""
    props = {"mac": PC_REPO / "Mac" / "ArupaDelivery.props"}.get(c.os)
    if not props or not props.exists():
        return
    rel = re.findall(r'RelativePath="([^"$]+)"',
                     props.read_text(encoding="utf-8", errors="replace"))
    missing = []
    for r in rel:
        probe = ("kernel/" + r[len("macKernel/"):]) if r.startswith("macKernel/") else r
        if not (stage / probe).exists():
            missing.append(r)
    if missing:
        err("交付包缺 PC 侧必需件（PC 编译第一步就会失败）:\n  " + "\n  ".join(missing)
            + f"\n  清单来源: {props}")
    log(f"PC 必需件校验通过（{len(rel)} 件，清单来自 {props.name}）")


def write_manifest(c: Ctx, stage: Path, n: int, kernel_dir: Path):
    files = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    kernel_head = out(["git", "rev-parse", "--short", "HEAD"], cwd=KERNEL_REPO) or "?"
    dirty = out(["git", "status", "--porcelain"], cwd=KERNEL_REPO)
    text = [
        f"# 交付清单 {c.dist_name(n)}",
        "",
        f"- 项目: {c.project}（nomad 内核）",
        f"- 平台: {c.os} / {c.arch} / {c.link}",
        f"- Chromium 版本: {c.ver}",
        f"- 交付次数: {n}",
        f"- 内核仓 HEAD: {kernel_head}{'（工作区有未提交改动）' if dirty else ''}",
        f"- 构建目录: {c.out_dir}",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 件清单",
        "",
    ]
    text += [f"- `{f}`" for f in files]
    (stage / "MANIFEST.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    (stage / "README.md").write_text(
        f"# {c.dist_name(n)}\n\n"
        f"nomad 内核交付包（{c.os}/{c.arch}/{c.link}，Chromium {c.ver}）。\n"
        f"件清单见 MANIFEST.md，校验见 SHA256SUMS.txt，门禁记录见同名 .gate.json。\n",
        encoding="utf-8")


def do_package(c: Ctx):
    if c.link != "static":
        err(f"只有 static 可出交付包（当前 {c.link}）—— 组件构建产物离开构建树就不可加载")
    preflight(c)
    if not c.out_dir.is_dir():
        err(f"构建目录不存在: {c.out_dir}（先跑: python3 scripts/build.py kernel build）")

    # v8 快照文件名各平台不同（Android 是 _64/_32），不能用统一的 {v8} 展开
    snap = v8_snapshot_name(c)

    def _fmt(items):
        return [snap if "{v8}" in i else i.format(v8=V8_ARCH.get(c.arch, c.arch))
                for i in items]

    required = _fmt(ARTIFACTS[c.os]["required"])
    optional = _fmt(ARTIFACTS[c.os]["optional"])

    # 门禁先于编号消耗：被拦下的误跑不该白跳一个号
    verify_freshness(c, required)
    n = next_delivery_no(c)
    name = c.dist_name(n)
    stage = DIST_ROOT / name
    zip_path = DIST_ROOT / f"{name}.zip"
    if stage.exists() or zip_path.exists():
        err(f"交付包已存在，拒绝覆盖: {stage}（换 --delivery-n 或先清理）")

    if common.DRY_RUN:
        log(f"(dry-run) 将出交付包: {stage} -> {zip_path}"
            f"（必带件: {' '.join(required)}）")
        return None
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    kernel_dir = stage / "kernel"
    if c.os == "android":
        kernel_dir = kernel_dir / c.arch      # 与上游一致：kernel/<abi>/...
    kernel_dir.mkdir(parents=True, exist_ok=True)

    for f in required:
        src = c.out_dir / f
        if not src.exists():
            err(f"必带件缺失: {f}（构建目录 {c.out_dir}）")
        dst = kernel_dir / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"  必带: {f} ({human_size(dst)})")
    for f in optional:
        src = c.out_dir / f
        if src.exists():
            dst = kernel_dir / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            log(f"  可选: {f} ({human_size(dst)})")

    write_delivery_markers(c, kernel_dir, n)   # PC 宿主硬校验项
    copy_assets(c, stage, kernel_dir)
    if c.os == "android":
        # AAR 组装 + 无法自编的框架层件（要赶在 write_manifest 前，好让清单收录）
        ref = stage_android_delivery(c, kernel_dir)
        write_capability_manifest(c, stage, n, ref)
    verify_pc_requirements(c, stage)
    write_manifest(c, stage, n, kernel_dir)
    write_sha256sums(stage)

    purge_previous_deliveries(c, keep=name)
    zip_dir(stage, zip_path, mac_keep_symlink=(c.os == "mac"))
    record_delivery(c, n, stage, sorted(p.relative_to(stage).as_posix()
                                        for p in stage.rglob("*") if p.is_file()))

    # 上层 PC 编译按这两个标记取"当前已验收的交付包"
    (DIST_ROOT / ".delivery-build").write_text(name + "\n", encoding="utf-8")
    if c.os == "mac":
        (DIST_ROOT / "ArupaMacDelivery.props").write_text(
            "<Project>\n  <PropertyGroup>\n"
            f"    <ArupaDeliveryRoot>{stage}</ArupaDeliveryRoot>\n"
            "  </PropertyGroup>\n</Project>\n", encoding="utf-8")

    log(f"交付包: {zip_path} ({human_size(zip_path)})")
    log(f"交付根: {stage}")
    return stage


ACTIONS = ("down", "gen", "build", "test", "package")


def print_delivery(c: Ctx):
    """给 PC 编译用：打印当前已验收的交付包根。"""
    f = DIST_ROOT / ".delivery-build"
    name = f.read_text(encoding="utf-8").strip() if f.exists() else ""
    if name and (DIST_ROOT / name).is_dir():
        print(DIST_ROOT / name)
        return 0
    pat = re.compile(r"^" + re.escape(c.dist_pre) + r"-(\d+)$")
    best = None
    if DIST_ROOT.is_dir():
        for p in DIST_ROOT.iterdir():
            m = pat.match(p.name)
            if m and p.is_dir():
                best = max(best or (0, p), (int(m.group(1)), p))
    if not best:
        err(f"没有已验收的交付包（先跑: python3 scripts/build.py kernel package）")
    print(best[1])
    return 0


def run_kernel(c: Ctx, actions: list):
    for a in actions:
        if a == "down":
            do_down(c)
        elif a == "gen":
            do_gen(c)
        elif a == "build":
            do_build(c)
        elif a == "test":
            do_test(c)
        elif a == "package":
            do_package(c)
