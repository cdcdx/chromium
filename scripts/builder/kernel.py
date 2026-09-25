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

import os
import re
import shutil
import time
from pathlib import Path

from .common import (SRC, KERNEL_REPO, DIST_ROOT, TOOLS_DIR, Ctx, build_cmd, cget,
                     err, gn_gen, git, log, out, probe_steps, purge_previous_deliveries,
                     run, warn, write_args_gn, write_sha256sums, zip_dir, extra_gn_args,
                     ensure_depot_tools, next_delivery_no, record_delivery, human_size)
from . import common

MODULE_RELDIR = "chrome/browser/arupa_desktop"
KERNEL_TARGET = f"{MODULE_RELDIR}:arupa_kernel"
RENDER_TARGET = f"{MODULE_RELDIR}:render"

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
                     "resources/inspector_overlay/inspector_overlay_resources.grd"],
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
        args += [("target_os", '"android"')]
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
    return targets


# ── 编译 ────────────────────────────────────────────────────────────────────
def do_build(c: Ctx):
    preflight(c)
    ensure_depot_tools(c.cfg)
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
    if c.os == "mac":
        alias = stage / "macKernel"
        if not alias.exists():
            alias.symlink_to("kernel", target_is_directory=True)   # PC 侧按 macKernel 取件
            log("  软链 macKernel -> kernel（PC 侧按此名取内核）")


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

    required = [r.format(v8=V8_ARCH.get(c.arch, c.arch)) for r in ARTIFACTS[c.os]["required"]]
    optional = [o.format(v8=V8_ARCH.get(c.arch, c.arch)) for o in ARTIFACTS[c.os]["optional"]]

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

    copy_assets(c, stage, kernel_dir)
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
