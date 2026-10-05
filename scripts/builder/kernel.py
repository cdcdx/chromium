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

import hashlib
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
                     run_build, ensure_android_native_deps, MULTI_ARCH,
                     ANDROID_MULTI_ARCHS, SCRIPTS_DIR, WORKSPACE_ROOT)
from . import common

MODULE_RELDIR = "chrome/browser/arupa_desktop"
KERNEL_TARGET = f"{MODULE_RELDIR}:arupa_kernel"
RENDER_TARGET = f"{MODULE_RELDIR}:render"
# 原生宿主形态: 内核静态链进浏览器进程 EXE，子进程由**同一个 EXE** 拉起。
# 三平台都能编这个目标，产物名随平台（arupa_desktop.exe / arupa_desktop）；
# 是否编、是否随交付一律以**构建图里有没有该目标**为准 —— 老内核仓没有它，未启用的
# 平台交付根也不带它，两种情况都表现为"图里没有"，行为一致（见 desktop_host_in_build）。
DESKTOP_TARGET = f"{MODULE_RELDIR}:arupa_desktop"
_DESKTOP_ARTIFACT_BY_OS = {
    "win": "arupa_desktop.exe",
    "mac": "arupa_desktop",
    "linux": "arupa_desktop",
}


def desktop_artifact(c: Ctx) -> str:
    """原生宿主产物名（随平台）。与 browser.py 的 desktop_host_name 同源约定。"""
    return _DESKTOP_ARTIFACT_BY_OS.get(c.os, "arupa_desktop")
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
        "required": ["libarupa_kernel.dylib", "arupa_render", "arupa_plugin_host"],
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
        "required": ["arupa_kernel.dll", "arupa_render.exe", "arupa_plugin_host.exe"],
        "optional": ["icudtl.dat", "snapshot_blob.bin", "v8_context_snapshot.{v8}.bin",
                     "content_shell.pak", "devtools_resources.pak", "shell_resources.pak",
                     "libEGL.dll", "libGLESv2.dll", "vk_swiftshader.dll",
                     "vk_swiftshader_icd.json"],
    },
    "linux": {
        "required": ["libarupa_kernel.so", "arupa_render", "arupa_plugin_host"],
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

# ── Android 门面 Java 层（classes.jar）──────────────────────────────────────
# 交付 aar 的 classes.jar **必须由本次自编产物生成**，不能沿用官方交付件那份：
# J/N.WHOLE_HASH 由该架构的 native 注册全集算出，官方 jar 的 J/N 是官方 .so
# 的那一份 —— 装到自编 .so 上，JNI_OnLoad 期 JniZero
# .crashIfMultiplexingMisaligned 直接抛异常 abort（进程起来 2s 就死）。
# 组成：dist_jar(arupa_java_face) 闭包 + java/android/facade-bin 门面实现
#       + 该架构 jni_registration.srcjar 里的最终 GEN_JNI / J.N（带 switch 号）。
JAVA_FACE_TARGET = f"{MODULE_RELDIR}:arupa_java_face"
JAVA_FACE_JAR = "arupa_java_face.jar"
FACADE_BIN = "java/android/facade-bin"
# 最终版 GEN_JNI / J.N 的源（generate_final_jni 产物，与 .so 同源）
JNI_REG_SRCJAR = "gen/chrome/browser/arupa_desktop/arupa_kernel__jni_registration.srcjar"
# native 侧同一哈希的权威出处（kJniZeroHashWhole）—— 门禁用它校 Java 侧
JNI_REG_CC = "gen/jni_headers/chrome/browser/arupa_desktop/arupa_kernel__jni_registration.cc"


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


V8_TLS_ARG_MARKER = "  v8_tls_used_in_library = false"
V8_TLS_USE_MARKER = "  if (v8_tls_used_in_library) {"


def patch_v8_tls():
    """给 src/v8/BUILD.gn 加 v8_tls_used_in_library 开关（幂等）。

    Linux 非 component 构建里 arupa_kernel 是 dlopen 的 shared_library，整棵 v8
    静态链进 .so；v8 默认 tls_model="local-exec"（Linux 非 Android 分支），
    于是每个 v8 对象都带 R_X86_64_TPOFF32，mold 直接拒绝链接（报
    "recompile with -fPIC" 是误导，不是 PIC 问题）。上游只留了
    v8_monolithic && v8_monolithic_for_shared_library 这一个入口，而它连带要求
    v8_use_external_startup_data=false（改动快照形态），故这里加一个独立开关。
    """
    f = SRC / "v8" / "BUILD.gn"
    if not f.exists():
        err(f"缺 {f}（源码树不完整？）")
    src = f.read_text(encoding="utf-8", errors="replace")
    if V8_TLS_USE_MARKER in src and V8_TLS_ARG_MARKER in src:
        log("src/v8/BUILD.gn 已有 v8_tls_used_in_library 开关")
        return
    if common.DRY_RUN:
        log("(dry-run) 给 src/v8/BUILD.gn 加 v8_tls_used_in_library 开关")
        return
    if V8_TLS_ARG_MARKER not in src:
        anchor = "  v8_monolithic_for_shared_library = false\n"
        if anchor not in src:
            err("src/v8/BUILD.gn 里找不到 v8_monolithic_for_shared_library 声明（v8 结构变了？）")
        src = src.replace(
            anchor,
            anchor + "\n"
            "  # arupa: 非 component 构建把 v8 静态链进 dlopen 的 .so 时，v8 默认的\n"
            "  # tls_model=\"local-exec\"（TPOFF32）在共享对象里非法 —— mold/ld 直接报错。\n"
            "  # 与 v8_monolithic_for_shared_library 同效（V8_TLS_USED_IN_LIBRARY →\n"
            "  # local-dynamic + getter 外置），但不要求 v8_monolithic /\n"
            "  # v8_use_external_startup_data=false，故不改变快照形态。\n"
            "  v8_tls_used_in_library = false\n", 1)
    if V8_TLS_USE_MARKER not in src:
        use = ('  if (v8_monolithic && v8_monolithic_for_shared_library) {\n'
               '    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n'
               '  }\n')
        if use not in src:
            err("src/v8/BUILD.gn 里找不到 v8_monolithic_for_shared_library 的 defines 块（v8 结构变了？）")
        src = src.replace(
            use,
            use + '\n  if (v8_tls_used_in_library) {\n'
            '    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n  }\n', 1)
    f.write_text(src, encoding="utf-8")
    log("已给 src/v8/BUILD.gn 加 v8_tls_used_in_library 开关")


def ensure_build_graph(c: Ctx):
    """构建图里没有本项目内核目标就补 patch + gen —— 否则 ninja: unknown target。"""
    if target_in_graph(c, KERNEL_TARGET):
        log("构建图已含本项目内核目标")
        return
    warn("构建图缺本项目内核目标（换过挂载点/别人 gen 过/目录刚建）—— 补 patch + gn gen")
    patch_gn_all()
    patch_v8_tls()
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
    patch_v8_tls()
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
    if c.os == "linux":
        # arupa_kernel 在 Linux 是 dlopen 的 shared_library，blink 与 v8 都静态链进
        # .so。非 component 构建下两者默认的 TLS 模型（blink: local-exec 且 getter
        # 内联；v8: local-exec）会在 DSO 里产生 R_X86_64_TPOFF32，mold 直接拒绝链接
        # （"recompile with -fPIC" 是误导）。这两个开关让它们退回 local-dynamic。
        # Android 走 local-dynamic、Mac getter 外置、Win 用 initial-exec，均无需动。
        args += [("blink_heap_inside_shared_library", "true"),
                 ("v8_tls_used_in_library", "true")]
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


def desktop_host_in_build(c: Ctx) -> bool:
    """构建图里有没有原生宿主目标（arupa_desktop）。

    判据用**构建图**而不是平台/源码/开关: 目标进图才算数。三平台都能编这个目标，
    但它只在交付根的 deps 里被带上时才进图（scripts/build.py 生成的 delivery 目标
    默认只在 Windows 带）—— 所以老内核仓、以及未启用的平台，这里都是 False，行为
    与"根本没有这个目标"完全一致。
    """
    return target_in_graph(c, DESKTOP_TARGET)


def required_artifacts(c: Ctx) -> list:
    """必带件清单（{v8} 未展开，见 artifact_names）。

    启用原生宿主时**多一件**（arupa_desktop[.exe]），而不是把 arupa_kernel 内核模块 /
    arupa_render 换掉 —— 那两件仍是开发工具的依赖: Test/ArupaCdpHeadersProbe 在原生
    宿主形态下靠内核模块（把浏览器 EXE 当库 LoadLibrary 时 CRT 静态初始化不跑，C API
    起不来）。摘掉它们的时机 = 工具改完，属独立改动。
    """
    req = list(ARTIFACTS[c.os]["required"])
    if desktop_host_in_build(c):
        req.append(desktop_artifact(c))
    return req


def resolve_targets(c: Ctx):
    targets = [KERNEL_TARGET]
    if target_in_graph(c, RENDER_TARGET):
        targets.append(RENDER_TARGET)
    else:
        warn(f"构建图里没有 {RENDER_TARGET}（子进程薄壳）—— 只编内核本体，"
             f"打包时若缺 render 会直接拦下")
    if target_in_graph(c, DESKTOP_TARGET):
        # 三平台都编：原生宿主在 macOS/Linux 上不是沙箱必需（那边的沙箱无跨进程内存写），
        # 但形态可选（见 arupa_desktop_main.cc 头部）。目标进图 = 有人显式要它，就编。
        targets.append(DESKTOP_TARGET)
    elif c.os == "win":
        warn(f"构建图里没有 {DESKTOP_TARGET}（原生宿主）—— 交付走库式嵌入形态，"
             f"Windows 沙箱要靠 ARUPA_UNSAFE_DISABLE_SANDBOX 才起得来")
    if c.os == "android":
        # 门面 Java 层（classes.jar 主体）—— 与 .so 同源，出 AAR 必需。
        if target_in_graph(c, JAVA_FACE_TARGET):
            targets.append(JAVA_FACE_TARGET)
        else:
            warn(f"构建图里没有 {JAVA_FACE_TARGET}（门面 Java 层）—— AAR 将缺少"
                 f" classes.jar，无法出包")
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
        if not run_build(c, targets):
            err("编译失败")

    required = [r.format(v8=V8_ARCH.get(c.arch, c.arch)) for r in required_artifacts(c)]
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
# 会影响 native 产物的源码扩展名。Java / 文档 / 脚本改动不该卡 .so 的新鲜度：
# 2026-09-27 实际踩过——只改了 ArupaKernelVersion.java，出包门禁却因为 .so "比
# 源码旧"而拒绝出包（.so 内容其实是对的），只能 touch 产物绕过。
NATIVE_SRC_EXTS = (".cc", ".c", ".h", ".hpp", ".mm", ".gn", ".gni", ".inc", ".def", ".rc")
NATIVE_ART_EXTS = (".so", ".dylib", ".dll")


def newest_source_mtime(repo: Path, exts: tuple = ()) -> float:
    files = out(["git", "ls-files"], cwd=repo).splitlines()
    mt = 0.0
    for f in files:
        if exts and not f.endswith(exts):
            continue
        p = repo / f
        if p.exists():
            mt = max(mt, p.stat().st_mtime)
    return mt


def verify_freshness(c: Ctx, required: list):
    """产物必须由当前源码编出来 —— 挡住"旧 dylib 换新交付号"。

    判定分两类（见文件头 NATIVE_SRC_EXTS 的说明）：
      · native 产物（.so/.dylib/.dll）只对 native 源码（.cc/.h/.gn…）敏感；
      · 其它产物（.bin/.pak…）以及"根本没有 native 产物"时，按全部源码判定。
    只改 Java/文档时放行并给提示，避免无意义的拦截。
    """
    art = [c.out_dir / f for f in required]
    art = [p for p in art if p.exists()]
    if not art:
        if common.DRY_RUN:
            warn("(dry-run) 构建目录里还没有产物 —— 跳过新鲜度校验")
            return
        err("构建目录里没有产物，先 build: python3 scripts/build.py kernel build")
    src_all = newest_source_mtime(KERNEL_REPO)
    src_native = newest_source_mtime(KERNEL_REPO, NATIVE_SRC_EXTS)
    oldest_all = min(p.stat().st_mtime for p in art)
    native_art = [p for p in art if p.suffix in NATIVE_ART_EXTS]
    oldest_native = min((p.stat().st_mtime for p in native_art), default=None)

    def ts(v):
        return time.strftime('%F %T', time.localtime(v))

    stale_native = oldest_native is not None and oldest_native < src_native
    # 没有 native 产物时退回"全部源码"口径
    stale_other = oldest_all < src_all
    if stale_native or (oldest_native is None and stale_other):
        msg = (f"产物比源码旧 —— 拒绝打成新交付号（编完又改了源码？）:\n"
               f"  native 源码最新: {ts(src_native)}\n"
               f"  native 最老产物: {ts(oldest_native) if oldest_native else '-'}\n"
               f"  全部源码最新  : {ts(src_all)}\n"
               f"  最老产物      : {ts(oldest_all)}\n"
               f"  修: 重新 python3 scripts/build.py kernel build 再打包")
        if c.gate:
            err(msg)
        warn("门禁已跳过（--no-gate）:\n  " + msg)
    elif stale_other:
        warn("产物比部分源码旧，但 native 源码没动（只改了 Java/文档/脚本）"
             "—— 放行：\n"
             f"  全部源码最新  : {ts(src_all)}\n"
             f"  native 源码最新: {ts(src_native)}\n"
             f"  最老产物      : {ts(oldest_all)}")
    else:
        log(f"产物新鲜度校验通过（native 产物 >= native 源码 {ts(src_native)}）")


def _copy_stage_assets(c: Ctx, stage: Path):
    """包级平台无关件 —— docs / probe-plugin 等，各 ABI 共用一份。

    Android 交付包不带 dotnet/ 与 include/（dotnet 是 PC 侧 C# 绑定、include 是
    C ABI 头，都只服务 PC 宿主）—— 参照上游 arupa-android-* 的包结构。
    """
    pkg = KERNEL_REPO / "package"
    subs = ["docs"] if c.os == "android" else ["docs", "dotnet"]
    for sub in subs:
        src = pkg / sub
        if src.is_dir():
            shutil.copytree(src, stage / sub, dirs_exist_ok=True)
            log(f"  收录 {sub}/")
        else:
            warn(f"内核仓缺 {src}（平台无关件），跳过")
    if c.os != "android":
        # C ABI 头只服务 PC 宿主（dotnet 绑定按它生成 P/Invoke 声明）
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


def copy_gen_paks(c: Ctx, kernel_dir: Path) -> int:
    """把构建树里 gen/ 下的 *.pak 按相对路径收进交付包的 kernel/gen/。

    坑: 内核的资源加载（arupa_content_main_delegate.cc:270-305）先找 <dir>/xxx.pak，
    再回退 <dir>/gen/extensions/.../xxx.pak。只带根目录那三个 pak 的话，
    extensions_strings_*、extensions_renderer_generated_resources、
    chrome/browser_resources、ui/webui/resources、components/strings/* 全都不在包里
    —— 渲染进程起来先打一串 [arupa][res] WARNING，随后在初始化里命中内核 CHECK，
    Windows 上表现为 arupa_render.exe 崩在 arupa_kernel.dll、异常码 0x80000003
    (STATUS_BREAKPOINT)，从崩溃表象完全看不出是"缺资源"。
    """
    if c.os == "android":
        return 0        # Android 的资源走 AAR/骨架里的 arupa_kernel.pak，不吃 gen/ 树
    gen = c.out_dir / "gen"
    if not gen.is_dir():
        err(f"构建目录没有 gen/（{gen}）—— 运行时资源不会随包，宿主起渲染进程会缺 pak")
    n = 0
    total = 0
    for src in gen.rglob("*.pak"):
        dst = kernel_dir / "gen" / src.relative_to(gen)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
        total += src.stat().st_size
    if n:
        log(f"  资源: gen/**/*.pak {n} 个 / {total / 1048576:.1f} MB -> kernel/gen/")
    # 两个硬必需（口径同 package-arupa_desktop.sh）：缺了不是功能降级，是运行期
    # CHECK 崩 —— FB-P095（扩展层本地化错误）/ FB-P104（扩展 renderer 绑定 JS 不在）。
    for rel in ("gen/extensions/strings/extensions_strings_en-US.pak",
                "gen/extensions/extensions_renderer_generated_resources.pak"):
        if not (kernel_dir / rel).is_file():
            err(f"缺必需的扩展资源: {rel}（构建目录 {gen} 里没有 —— 先编内核资源目标）")
    return n


def _copy_kernel_assets(c: Ctx, kernel_dir: Path):
    """随每个 ABI 内核走的件（plugin-runtime 打进 AAR 旁的 kernel/<abi>/）。"""
    src = KERNEL_REPO / "package" / "plugin-runtime"
    if src.is_dir():
        shutil.copytree(src, kernel_dir / "plugin-runtime", dirs_exist_ok=True)
        log(f"  收录 plugin-runtime/ -> kernel/{c.arch}/")
    else:
        warn(f"内核仓缺 {src}（插件运行时），跳过")


def copy_assets(c: Ctx, stage: Path, kernel_dir: Path):
    """平台无关件取内核仓 package/ + public/（与编进 dylib 的那份同源）。"""
    _copy_stage_assets(c, stage)
    _copy_kernel_assets(c, kernel_dir)


# ── Android 交付件：AAR 组装 ──────────────────────────────────────────────────
#
# 原则：**没有骨架库目录**（原 scripts/builder/data/android-skeleton 已废弃）。
#   * AAR 的结构（有哪些条目、顺序、压缩方式、manifest / R.txt / public.txt）
#     是脚本自己记的常量 —— 见 AAR_MANIFEST_XML / AAR_ENTRY_SPEC。
#   * classes.jar 与 libarupakernel.so 每次由本仓自产（同源，带 WHOLE_HASH 门禁）。
#   * 只剩三件工作区无法自编，就地按优先级取用，用完不落任何缓存目录：
#       arupa_kernel_resources.apk <- 内核仓 package/android/（源码树内）
#       arupa_kernel.pak           <- App 仓库 app/src/main/assets/（产品内）
#       libarupapluginhost.so      <- App 仓库 app/libs/kernel/<abi>/arupa-kernel.aar
#     三件都缺时才回退 dist/arupa-android-* 官方交付件（一次性输入，不固化）。

SKELETON_GLOB = "arupa-android-*"      # 官方参考交付件（可选输入，不是出包前提）

AAR_MANIFEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<!--
  Minimal AndroidManifest for the arupa-kernel library AAR.
  Library AARs declare just their package; host apps merge their own
  activity / permission declarations into the resulting manifest.
-->
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
          package="co.arupa.kernel">

  <uses-sdk android:minSdkVersion="26" android:targetSdkVersion="35" />

  <!-- Library has no <application> entry; consumers add their own. -->

</manifest>
"""

# Chromium 树内 proguard flags —— 拼成 AAR 的 proguard.txt（消费方 R8 用）
PROGUARD_FLAGS_IN_TREE = ("base/android/proguard/chromium_apk.flags",)

# AAR 条目结构（顺序即写入顺序）：(条目名, 来源 kind, 压缩方式)
#   kind: text=脚本常量 / part=外取件 / classes=自产门面层 / so=自编内核
AAR_ENTRY_SPEC = (
    ("AndroidManifest.xml",            "text:AndroidManifest.xml",  zipfile.ZIP_STORED),
    ("R.txt",                          "text:",                     zipfile.ZIP_STORED),
    ("public.txt",                     "text:",                     zipfile.ZIP_STORED),
    ("proguard.txt",                   "part:proguard.txt",         zipfile.ZIP_STORED),
    ("jni/{jni}/libarupapluginhost.so", "part:libarupapluginhost.so", zipfile.ZIP_STORED),
    ("assets/arupa_kernel_resources.apk", "part:arupa_kernel_resources.apk", zipfile.ZIP_STORED),
    ("classes.jar",                    "classes",                   zipfile.ZIP_DEFLATED),
    ("jni/{jni}/libarupakernel.so",    "so",                        zipfile.ZIP_DEFLATED),
)

# 需要从外部取的件（AAR 内 + 随包散装）
FRAMEWORK_PARTS = ("proguard.txt", "libarupapluginhost.so",
                   "arupa_kernel_resources.apk", "arupa_kernel.pak")


def find_reference_delivery(c: Ctx) -> Path | None:
    """找官方参考交付件 —— 只作最后回退，取完不缓存。没有也不致命。"""
    if not DIST_ROOT.is_dir():
        return None
    cands = [p for p in sorted(DIST_ROOT.glob(SKELETON_GLOB))
             if (p / "kernel" / c.arch / "arupa-kernel.aar").is_file()]
    return cands[-1] if cands else None


def app_kernel_aar(c: Ctx) -> Path | None:
    """App 仓库里现成的内核 AAR —— 产品内的权威框架层来源。"""
    p = common.ANDROID_REPO / "app" / "libs" / "kernel" / c.arch / "arupa-kernel.aar"
    return p if p.is_file() else None


def _read_aar_entry(aar: Path, entry: str) -> bytes | None:
    try:
        with zipfile.ZipFile(aar) as z:
            return z.read(entry) if entry in z.namelist() else None
    except (zipfile.BadZipFile, OSError):
        return None


def framework_parts(c: Ctx) -> dict[str, bytes]:
    """取无法自编的框架件（字节），优先级：源码树 > 产品内 > 官方交付件。

    不写任何缓存目录 —— 每次出包现取。缺件返回空 dict 由调用方报警。"""
    jni = AAR_JNI_DIR.get(c.arch, c.arch)
    parts: dict[str, bytes] = {}

    def take(name: str, data: bytes | None, src: str) -> bool:
        if data is None:
            return False
        parts[name] = data
        log(f"  框架件 {name} <- {src} ({human_size_file(len(data))})")
        return True

    # 1) App 仓库（产品内）—— 与宿主 runtime-manifest 的校验值同源。
    #    ⚠ 唯独 resources.apk 不从这里（旧 AAR）继承：它必须与「生成 R 用」的那份
    #    完全同源，否则 R 常量整体错位。2026-09-27 事故：旧 AAR 里是官方交付件那份
    #    （integer/min_screen_width_bucket=0x7f0c0008，全表 2312 条），而 R 由内核仓
    #    那份（=0x7f0c003a，全表 12401 条）生成 → 运行时按 0x3a 查表抛
    #    Resources$NotFoundException → DeviceFormFactor.detectScreenWidthBucket
    #    → 任意 loadUrl 即 FATAL。见第 2 步（唯一来源）。
    pak = common.ANDROID_REPO / "app" / "src" / "main" / "assets" / "arupa_kernel.pak"
    take("arupa_kernel.pak", pak.read_bytes() if pak.is_file() else None,
         "nomadbrowser.android/app/src/main/assets/" if pak.is_file() else "")
    aar = app_kernel_aar(c)
    if aar is not None:
        rel = f"nomadbrowser.android/app/libs/kernel/{c.arch}/arupa-kernel.aar"
        take("libarupapluginhost.so",
             _read_aar_entry(aar, f"jni/{jni}/libarupapluginhost.so"), rel)
        take("proguard.txt", _read_aar_entry(aar, "proguard.txt"), rel)

    # 2) 资源包：唯一来源 = 内核源码树 package/android/arupa_kernel_resources.apk
    #    （:arupa_kernel_res_r 的 GN input 就是它 → 编译期 R 与运行时资源表同源）。
    #    与旧 AAR / 官方交付件里那份是**不同的资源表**（条目数与编号都不同），
    #    混用会让 R 常量整体错位，务必只走这一条路。
    res = KERNEL_REPO / "package" / "android" / "arupa_kernel_resources.apk"
    if not res.is_file():
        err(f"缺内核资源包: {res}\n"
            f"  （:arupa_kernel_res_r 生成 org.chromium.*.R 用的就是它；"
            f"不能改用官方交付件里那份）")
    res_bytes = res.read_bytes()
    take("arupa_kernel_resources.apk", res_bytes,
         str(res.relative_to(WORKSPACE_ROOT)))
    log(f"  资源包 sha256={hashlib.sha256(res_bytes).hexdigest()[:16]}"
        f"（与编译期 R 同源，勿换）")

    # 3) 回退：官方交付件（一次性输入，不固化）
    ref = find_reference_delivery(c)
    if ref is not None:
        for name in ("arupa_kernel.pak", "arupa_kernel_resources.apk"):
            if name in parts:
                continue
            f = ref / "kernel" / c.arch / name
            take(name, f.read_bytes() if f.is_file() else None, f"{ref.name}/kernel/{c.arch}/")
        if "libarupapluginhost.so" not in parts or "proguard.txt" not in parts:
            ref_aar = ref / "kernel" / c.arch / "arupa-kernel.aar"
            if ref_aar.is_file():
                if "libarupapluginhost.so" not in parts:
                    take("libarupapluginhost.so",
                         _read_aar_entry(ref_aar, f"jni/{jni}/libarupapluginhost.so"), ref.name)
                if "proguard.txt" not in parts:
                    take("proguard.txt", _read_aar_entry(ref_aar, "proguard.txt"), ref.name)

    # 4) proguard 最后回退：Chromium 树内 flags 拼接
    if "proguard.txt" not in parts:
        chunks = []
        for rel in PROGUARD_FLAGS_IN_TREE:
            f = SRC / rel
            if f.is_file():
                chunks.append(f.read_text(encoding="utf-8", errors="replace"))
        if chunks:
            parts["proguard.txt"] = "\n".join(chunks).encode()
            log("  框架件 proguard.txt <- Chromium 树内 flags 拼接")

    for name in FRAMEWORK_PARTS:
        if name not in parts:
            warn(f"框架件缺 {name}（源码树/App 仓库/官方件都没有）")
    return parts


def human_size_file(n: int) -> str:
    """字节数 -> 人类可读（common.human_size 只收 Path，这里按 int 走）。"""
    f = float(n)
    for unit in ("B", "K", "M", "G"):
        if f < 1024 or unit == "G":
            return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024
    return f"{n} B"


def aar_assemble(c: Ctx, parts: dict[str, bytes], classes_jar: Path,
                 so: Path, out: Path) -> None:
    """按 AAR_ENTRY_SPEC 组装 AAR —— 结构由脚本定义，不读任何骨架框架包。

    classes.jar 是本次自产的门面 Java 层（与 .so 同源，见 java_face_classes_jar）：
    若照搬官方 classes.jar，其 J/N.WHOLE_HASH 属于官方 .so，装到自编 .so 上
    JNI_OnLoad 期必 abort。"""
    jni = AAR_JNI_DIR.get(c.arch, c.arch)
    with zipfile.ZipFile(out, "w") as zout:
        for entry, kind, comp in AAR_ENTRY_SPEC:
            name = entry.format(jni=jni)
            if kind.startswith("text:"):
                data = (AAR_MANIFEST_XML if kind.endswith("AndroidManifest.xml")
                        else "").encode("utf-8")
            elif kind.startswith("part:"):
                data = parts.get(kind.split(":", 1)[1], b"")
            elif kind == "classes":
                data = classes_jar.read_bytes()
            elif kind == "so":
                data = so.read_bytes()
            else:
                continue
            zi = zipfile.ZipInfo(name)
            zi.compress_type = comp
            zout.writestr(zi, data)
            log(f"    + {name} ({human_size_file(len(data))})")


# ── 门面 Java 层（classes.jar）：按架构定型 ──────────────────────────────────
def jdk_tool(name: str) -> Path:
    """树内 JDK（chromium 自带 third_party/jdk）—— 不依赖系统 java 版本。"""
    p = SRC / "third_party" / "jdk" / "current" / "bin" / name
    if not p.exists():
        err(f"缺树内 JDK 工具: {p}")
    return p


def native_whole_hash(c: Ctx) -> int | None:
    """该架构 native 侧的 JNI Zero 全集哈希（kJniZeroHashWhole）。"""
    f = c.out_dir / JNI_REG_CC
    if not f.is_file():
        return None
    m = re.search(r"kJniZeroHashWhole\s*=\s*(-?\d+)LL",
                  f.read_text(encoding="utf-8", errors="replace"))
    return int(m.group(1)) if m else None


def jar_whole_hash(jar: Path) -> int | None:
    """classes.jar 里 J/N.class 的 WHOLE_HASH —— javap 读常量，不用手写解析器。"""
    import subprocess
    r = subprocess.run([str(jdk_tool("javap")), "-constants", "-p", "-cp", str(jar), "J.N"],
                       capture_output=True, text=True)
    m = re.search(r"WHOLE_HASH\s*=\s*(-?\d+)", r.stdout)
    return int(m.group(1)) if m else None


def downgrade_class_major(root: Path, from_v: int = 69, to_v: int = 65) -> int:
    """把 class 文件的 major version 从 from_v 改成 to_v（就地），返回改写数量。

    chromium 的 compile_java.py 硬编码 --release 25（树内 JDK 25），闭包 jar 是
    major 69；宿主 App 的 Gradle/AGP 固定在 JDK 21，javac 读到 69 会直接报
    "class file has wrong version 69.0, should be 65.0"。21↔25 之间 class 文件
    格式没有结构差异，改版本号标记即可。"""
    import struct
    n = 0
    for p in root.rglob("*.class"):
        b = p.read_bytes()
        if len(b) > 8 and struct.unpack(">H", b[6:8])[0] == from_v:
            p.write_bytes(b[:6] + struct.pack(">H", to_v) + b[8:])
            n += 1
    return n


def _desc_param_count(desc: str) -> int:
    """JVM 方法描述符参数个数。"""
    return len(re.findall(r"\[*L[^;]+;|\[*[BCDFIJSZ]", desc))


def _shell_natives_signatures(classes_jar: Path) -> dict[str, dict[str, int]]:
    """壳编译产物里各 `*$Natives` 接口的 方法名 -> 参数个数。"""
    import subprocess
    with zipfile.ZipFile(classes_jar) as z:
        names = [n[:-6].replace("/", ".") for n in z.namelist()
                 if n.endswith("$Natives.class")]
    if not names:
        return {}
    r = subprocess.run([str(jdk_tool("javap")), "-p", "-cp", str(classes_jar), *names],
                       capture_output=True, text=True)
    out: dict[str, dict[str, int]] = {}
    cur = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        m_cls = re.search(r"((?:\w+\.)*\w+\$Natives)\s*\{$", line)
        if m_cls:
            cur = m_cls.group(1)
            continue
        m_m = re.match(r"^[\w\s<>,\.\[\]$]*?\s(\w+)\(([^)]*)\);$", line)
        if cur and m_m:
            args = [a for a in m_m.group(2).split(",") if a.strip()]
            out.setdefault(cur, {})[m_m.group(1)] = len(args)
    return out


def _facade_natives_calls(facade_bin: Path) -> dict[str, dict[str, int]]:
    """facade-bin 二进制里对 `*$Natives.*` 的调用 -> 方法名 / 参数个数。"""
    import subprocess
    classes = sorted(
        p.relative_to(facade_bin).with_suffix("").as_posix().replace("/", ".")
        for p in facade_bin.rglob("*.class"))
    if not classes:
        return {}
    r = subprocess.run([str(jdk_tool("javap")), "-p", "-c", "-cp", str(facade_bin),
                        *classes], capture_output=True, text=True)
    out: dict[str, dict[str, int]] = {}
    for line in r.stdout.splitlines():
        # javap -c 里接口用**斜杠**全限定名（co/arupa/...$Natives）。
        m = re.search(r"InterfaceMethod ([\w/$]+?\$Natives)\.(\w+):\(([^)]*)\)", line)
        if m:
            cls = m.group(1).replace("/", ".")
            out.setdefault(cls, {})[m.group(2)] = _desc_param_count(m.group(3))
    return out


def check_facade_natives_signatures(classes_jar: Path, facade_bin: Path) -> None:
    """门禁：facade-bin 对 `*$Natives.*` 的每次调用都必须能在壳编译的声明里找到，
    且参数个数一致。

    为什么需要：壳源码（`generate_jni` 的输入，决定 native 生成头与注册表）与
    facade-bin（App 侧真正使用的二进制）是两套来源，签名漂移编译期查不出来，只有
    真调用才 NoSuchMethodError。2026-09-27 实际踩过：
    `ArupaPluginBridge$Natives.revokeActiveTabTab` 壳声明 2 参、facade-bin 1 参
    （探针当时又恰好没覆盖它，一直到人工比对才发现）。
    """
    shell = _shell_natives_signatures(classes_jar)
    facade = _facade_natives_calls(facade_bin)
    # 自检：任一侧解析为空就说明门禁自己坏了（javap 输出格式变了 / 路径不对），
    # 不能静默放行 —— 否则门禁形同虚设（本函数第一版就踩过：正则要求点分名，
    # 而 javap -c 给的是斜杠名，于是"0 个接口 / 0 处调用"仍然报通过）。
    if not shell or not facade:
        err(f"门面 $Natives 签名门禁自身失效：壳侧接口 {len(shell)} 个、"
            f"facade-bin 调用接口 {len(facade)} 个。\n"
            f"  壳 jar: {classes_jar}\n  facade: {facade_bin}\n"
            f"  多半是 javap 输出格式变化或路径不对，先排查门禁本身。")
    bad: list[str] = []
    for cls, methods in sorted(facade.items()):
        declared = shell.get(cls)
        if declared is None:
            bad.append(f"{cls}: facade-bin 调用了它，但壳编译产物里没有这个 "
                       f"$Natives 接口")
            continue
        for name, argc in sorted(methods.items()):
            want = declared.get(name)
            if want is None:
                bad.append(f"{cls}.{name}: facade-bin 调用（{argc} 参），"
                           f"壳声明里没有该方法")
            elif want != argc:
                bad.append(f"{cls}.{name}: facade-bin 调用 {argc} 参，"
                           f"壳声明 {want} 参")
    if bad:
        err("门面 $Natives 签名漂移（壳源码 vs facade-bin 二进制）:\n  " +
            "\n  ".join(bad) +
            "\n  修: 改 java/android/src/co/arupa/**/<Bridge>.java 里 @NativeMethods "
            "的声明，使其与 facade-bin 的实际调用一致，再重编。")
    log(f"  门面 $Natives 签名门禁通过（{len(facade)} 个接口 / "
        f"{sum(len(m) for m in facade.values())} 处调用）")


def java_face_classes_jar(c: Ctx) -> Path:
    """按架构产出 AAR 用的 classes.jar。

      自产闭包(dist_jar arupa_java_face: org.chromium/org.jni_zero 全量 + 门面
      壳类) + 门面实现二进制(java/android/facade-bin, 覆盖同名壳类) + 该架构
      jni_registration.srcjar 的最终 GEN_JNI / J.N（带 switch 号）。

      最后做同源门禁：J/N.WHOLE_HASH 必须等于该架构 native 的
      kJniZeroHashWhole —— 这是线上"加载即 abort"那次事故的唯一防线。"""
    face = c.out_dir / JAVA_FACE_JAR
    if not face.is_file():
        err(f"缺自产门面 jar: {face}\n"
            f"  先编: python3 scripts/build.py kernel build --os android --arch {c.arch}\n"
            f"  ⚠ 不能退回骨架库那份官方 classes.jar：它的 J/N.WHOLE_HASH 属于官方 .so，"
            f"装到自编 .so 上 JNI_OnLoad 期必 abort。")
    facade_bin = KERNEL_REPO / FACADE_BIN
    if not facade_bin.is_dir():
        err(f"缺门面实现二进制: {facade_bin}")

    work = c.out_dir / f".java-face-{c.arch}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    jar, javac = jdk_tool("jar"), jdk_tool("javac")
    # 1) 展开闭包 jar
    if not run([str(jar), "xf", str(face)], cwd=work):
        err(f"展开失败: {face}")
    # 2) 叠加门面实现二进制（co/arupa/**；*Jni/$Natives 是构建生成件，已在闭包里）
    if (facade_bin / "co").is_dir():
        shutil.copytree(facade_bin / "co", work / "co", dirs_exist_ok=True)
    else:
        err(f"门面二进制缺 co/ 目录: {facade_bin}")
    # 3) 最终 GEN_JNI / J.N —— 带 switch 号，与 .so 的 J.N 分发表同源
    srcjar = c.out_dir / JNI_REG_SRCJAR
    if not srcjar.is_file():
        err(f"缺 JNI 注册源: {srcjar}（generate_final_jni 产物）")
    if not run([str(jar), "xf", str(srcjar)], cwd=work):
        err(f"展开失败: {srcjar}")
    if not run([str(javac), "--release", "17", "-nowarn", "-encoding", "UTF-8",
                "-cp", str(work), "-d", str(work),
                str(work / "org" / "jni_zero" / "GEN_JNI.java"),
                str(work / "J" / "N.java")], cwd=work):
        err(f"GEN_JNI/J.N 编译失败（架构 {c.arch}, {srcjar}）")
    for leftover in (work / "org" / "jni_zero" / "GEN_JNI.java", work / "J" / "N.java"):
        leftover.unlink(missing_ok=True)

    # 4) 降 class 主版本号：chromium 用树内 JDK 25 编译（compile_java.py 硬编码
    #    --release 25），产物是 major 69；宿主 App 的 Gradle/AGP 跑在 JDK 21 上，
    #    javac 读到 69 直接 "class file has wrong version 69.0, should be 65.0"。
    #    这里把 69 降到 65（Java 21）—— 只改版本号标记，字节码结构在 21↔25 之间
    #    没有差异。
    n69 = downgrade_class_major(work)
    if n69:
        log(f"  class 版本: {n69} 个 69.0 -> 65.0（宿主 Gradle 跑 JDK 21）")

    out = c.out_dir / f"classes-{c.arch}.jar"
    if out.exists():
        out.unlink()
    if not run([str(jar), "cf", str(out), "."], cwd=work):
        err(f"classes.jar 打包失败（架构 {c.arch}）")
    shutil.rmtree(work)

    # 4) 同源门禁
    nh, jh = native_whole_hash(c), jar_whole_hash(out)
    if nh is None:
        err(f"读不到 native 侧哈希: {c.out_dir / JNI_REG_CC}（无法确认 classes.jar 与 .so 同源）")
    if jh != nh:
        err(f"J/N 哈希不匹配（{c.arch}）: classes.jar={jh} native={nh}\n"
            f"  装到设备上 JNI_OnLoad 期就会被 crashIfMultiplexingMisaligned abort。")
    log(f"  classes.jar（{c.arch}）: {human_size(out)}，"
        f"J/N.WHOLE_HASH={jh} == native kJniZeroHashWhole ✓")

    # 5) 门禁：门面二进制调用的 $Natives 签名必须与壳编译声明一致
    check_facade_natives_signatures(out, facade_bin)
    return out


def stage_android_delivery(c: Ctx, kernel_dir: Path) -> Path | None:
    """Android 平台专属补齐：组装 AAR + 带入无法自编的框架件（不落骨架目录）。"""
    so = kernel_dir / "libarupa_kernel.so"
    if not so.is_file():
        err(f"缺自编内核 .so: {so}")
    parts = framework_parts(c)
    classes = java_face_classes_jar(c)
    aar_assemble(c, parts, classes, so, kernel_dir / "arupa-kernel.aar")
    jni_dir = AAR_JNI_DIR.get(c.arch, c.arch)
    log(f"  组装: arupa-kernel.aar（jni/{jni_dir}/libarupakernel.so <- 自编 "
        f"{human_size(so)}，classes.jar <- 自产门面层(hash 同源)，"
        f"框架件 {len(parts)} 件就地取用）")

    # 资源包同源门禁：AAR 内 assets/arupa_kernel_resources.apk 必须与
    # 「编译期生成 R 用」的那份逐字节相同 —— R 常量是资源表 entry 编号，
    # 两份表混用会让所有 R.* 指向错误的资源（2026-09-27 loadUrl 即 FATAL 的根因）。
    res_src = KERNEL_REPO / "package" / "android" / "arupa_kernel_resources.apk"
    with zipfile.ZipFile(kernel_dir / "arupa-kernel.aar") as z:
        in_aar = z.read("assets/arupa_kernel_resources.apk")
    want = res_src.read_bytes() if res_src.is_file() else b""
    if hashlib.sha256(in_aar).hexdigest() != hashlib.sha256(want).hexdigest():
        err(f"AAR 内资源包与编译期 R 的来源不同源（{c.arch}）:\n"
            f"  AAR 内        : {hashlib.sha256(in_aar).hexdigest()[:16]}\n"
            f"  R 的来源      : {hashlib.sha256(want).hexdigest()[:16]} ({res_src})\n"
            f"  两者资源表编号不同会让所有 R.* 错位（DeviceFormFactor 一读就崩）。")
    log(f"  资源包同源门禁通过: sha256={hashlib.sha256(in_aar).hexdigest()[:16]}")

    for name in ("arupa_kernel.pak", "arupa_kernel_resources.apk"):
        data = parts.get(name)
        if data:
            (kernel_dir / name).write_bytes(data)
            log(f"  随包: {name} ({human_size_file(len(data))})")
        else:
            warn(f"缺 {name}，跳过")
    return None


def write_capability_manifest(c: Ctx, stage: Path, n: int,
                              ref: Path | None = None) -> None:
    """能力清单 —— 与上游 capability-manifest.json 同 schema，locked_sha256 填本次产物。

    ref 仅用于取 ABI 版本（随上游，不自创）；没有就用默认值。骨架库取消后
    改为现取官方交付件，不再依赖任何固化目录。"""
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
    caps = []
    if ref is not None:
        caps.append(ref / "capability-manifest.json")
    rd = find_reference_delivery(c)
    if rd is not None:
        caps.append(rd / "capability-manifest.json")
    for cap in caps:                           # ABI 版本随上游，不自创
        if not cap.is_file():
            continue
        try:
            abi = json.loads(cap.read_text(encoding="utf-8")).get("abi", abi)
            break
        except json.JSONDecodeError:
            warn(f"参考交付件的 {cap.name} 解析失败，ABI 版本用默认值")
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
    # 合并包里两套 ABI 并列，写清楚是哪两套、各自从哪个构建目录来
    if c.arch == MULTI_ARCH:
        arch_desc = "+".join(ANDROID_MULTI_ARCHS)
        out_lines = [f"- 构建目录({a}): {sub_ctx(c, a).out_dir}" for a in ANDROID_MULTI_ARCHS]
    else:
        arch_desc = c.arch
        out_lines = [f"- 构建目录: {c.out_dir}"]
    text = [
        f"# 交付清单 {c.dist_name(n)}",
        "",
        f"- 项目: {c.project}（nomad 内核）",
        f"- 平台: {c.os} / {arch_desc} / {c.link}",
        f"- Chromium 版本: {c.ver}",
        f"- 交付次数: {n}",
        f"- 内核仓 HEAD: {kernel_head}{'（工作区有未提交改动）' if dirty else ''}",
        *out_lines,
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 件清单",
        "",
    ]
    text += [f"- `{f}`" for f in files]
    (stage / "MANIFEST.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    (stage / "README.md").write_text(
        f"# {c.dist_name(n)}\n\n"
        f"nomad 内核交付包（{c.os}/{arch_desc}/{c.link}，Chromium {c.ver}）。\n"
        f"件清单见 MANIFEST.md，校验见 SHA256SUMS.txt，门禁记录见同名 .gate.json。\n",
        encoding="utf-8")


def artifact_names(c: Ctx) -> tuple[list, list]:
    """必带 / 可选件清单 —— v8 快照文件名各平台不同（Android 是 _64/_32），
    不能用统一的 {v8} 展开。"""
    snap = v8_snapshot_name(c)

    def _fmt(items):
        return [snap if "{v8}" in i else i.format(v8=V8_ARCH.get(c.arch, c.arch))
                for i in items]

    return _fmt(required_artifacts(c)), _fmt(ARTIFACTS[c.os]["optional"])


def stage_kernel_files(c: Ctx, kernel_dir: Path):
    """把单个构建目录的产物收进 kernel/（或 kernel/<abi>/）。"""
    required, optional = artifact_names(c)
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


def sub_ctx(c: Ctx, arch: str) -> Ctx:
    """同一项目/版本下换架构 —— 合并包按 ABI 逐个处理，其余字段不动。"""
    import dataclasses
    return dataclasses.replace(c, arch=arch)


def package_android_multi(c: Ctx):
    """Android 合并交付包：arm64 与 x64 打进同一个包。

    目录结构与上游 arupa-android-* 一致 —— kernel/<abi>/ 并列，probe-plugin / docs /
    capability-manifest 等各 ABI 共用一份只放顶层：
        kernel/arm64/{arupa-kernel.aar,icudtl.dat,arupa_kernel.pak,snapshot_blob.bin,...}
        kernel/x64/{...}
        probe-plugin/  docs/  capability-manifest.json  MANIFEST.md  SHA256SUMS.txt
    """
    if c.os != "android":
        err(f"合并包只支持 android（当前: {c.os}）")
    subs = [sub_ctx(c, a) for a in ANDROID_MULTI_ARCHS]
    for s in subs:
        if not s.out_dir.is_dir():
            err(f"构建目录不存在: {s.out_dir}\n"
                f"  先跑: python3 scripts/build.py kernel build --os android --arch {s.arch}")
        required, _ = artifact_names(s)
        missing = [f for f in required if not (s.out_dir / f).exists()]
        if missing:
            err(f"{s.arch} 缺必带件: {' '.join(missing)}（构建目录 {s.out_dir}）")
        # 门禁先于编号消耗：被拦下的误跑不该白跳一个号
        verify_freshness(s, required)

    n = next_delivery_no(c)
    name = c.dist_name(n)
    stage = DIST_ROOT / name
    zip_path = DIST_ROOT / f"{name}.zip"
    if stage.exists() or zip_path.exists():
        err(f"交付包已存在，拒绝覆盖: {stage}（换 --delivery-n 或先清理）")
    if common.DRY_RUN:
        log(f"(dry-run) 将出合并交付包: {stage} -> {zip_path}"
            f"（ABI: {' / '.join(ANDROID_MULTI_ARCHS)}）")
        return None

    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    ref = None
    for s in subs:
        kernel_dir = stage / "kernel" / s.arch
        kernel_dir.mkdir(parents=True, exist_ok=True)
        log(f"[{s.arch}] 取自 {s.out_dir.name}")
        stage_kernel_files(s, kernel_dir)
        write_delivery_markers(s, kernel_dir, n)
        _copy_kernel_assets(s, kernel_dir)
        # AAR 组装 + 无法自编的框架层件（要赶在 write_manifest 前，好让清单收录）
        ref = stage_android_delivery(s, kernel_dir)
    _copy_stage_assets(c, stage)
    write_capability_manifest(c, stage, n, ref)
    verify_pc_requirements(c, stage)
    write_manifest(c, stage, n, None)
    write_sha256sums(stage)

    purge_previous_deliveries(c, keep=name)
    zip_dir(stage, zip_path, mac_keep_symlink=False)
    record_delivery(c, n, stage, sorted(p.relative_to(stage).as_posix()
                                        for p in stage.rglob("*") if p.is_file()))

    # 上层 PC 编译按这两个标记取"当前已验收的交付包"
    (DIST_ROOT / ".delivery-build").write_text(name + "\n", encoding="utf-8")

    log(f"交付包: {zip_path} ({human_size(zip_path)})")
    log(f"交付根: {stage}（含 {' / '.join(ANDROID_MULTI_ARCHS)} 两套内核）")
    return stage


def do_package(c: Ctx):
    if c.link != "static":
        err(f"只有 static 可出交付包（当前 {c.link}）—— 组件构建产物离开构建树就不可加载")
    preflight(c)
    if c.arch == MULTI_ARCH:
        return package_android_multi(c)
    if not c.out_dir.is_dir():
        err(f"构建目录不存在: {c.out_dir}（先跑: python3 scripts/build.py kernel build）")

    required, _ = artifact_names(c)
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

    stage_kernel_files(c, kernel_dir)

    copy_gen_paks(c, kernel_dir)        # gen/ 下的运行时资源（pak）—— 缺了渲染进程会崩
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
