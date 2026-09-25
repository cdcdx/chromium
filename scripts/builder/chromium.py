#!/usr/bin/env python3
# =============================================================================
#  Chromium 官方基线编译（对照基线，不带内核）
#
#  编译目录: out/chromium-<os>-<arch>-<ver>-<static|dynamic>
#  产物    : mac -> <out>/Chromium.app    win -> <out>/chrome.exe    linux -> <out>/chrome
#  动作    : down(gclient sync+hooks) / gen / build / all
#
#  与内核构建物理隔离（目录名不同 + 版本维度入名），不会串缓存。
# =============================================================================
from __future__ import annotations

import os
import re
from pathlib import Path

from .common import (SRC, HOST_OS, IS_WIN, Ctx, apply_developer_dir, cget, err,
                     gn_gen, log, out, run, warn, write_args_gn, extra_gn_args,
                     ensure_depot_tools, gclient_bin, human_size)

TARGET = "chrome"


def do_down(c: Ctx):
    """拉齐依赖 + 工具链（首次 / 换版本后跑）。"""
    if IS_WIN:
        os.environ.setdefault("DEPOT_TOOLS_WIN_TOOLCHAIN", "0")
    ensure_depot_tools(c.cfg)
    run([gclient_bin(), "sync", "--nohooks", "--jobs", str(c.jobs or 8)], cwd=SRC.parent)
    run([gclient_bin(), "runhooks"], cwd=SRC.parent)
    log("依赖同步完成")


def _mac_sdk_guard(c: Ctx):
    """sysroot 一漂就是全量重编（实测约 3 万步）—— 动手前先比对构建图里记录的 SDK。"""
    if c.os != "mac" or not (c.out_dir / "toolchain.ninja").exists():
        return
    cur = out(["xcrun", "-sdk", "macosx", "--show-sdk-path"])
    if not cur:
        warn("xcrun 取不到 macosx SDK —— 跳过漂移守卫")
        return
    want = Path(cur).name
    recorded = ""
    text = (c.out_dir / "toolchain.ninja").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"MacOSX[0-9]+\.[0-9]+\.sdk", text)
    if m:
        recorded = m.group(0)
    if not recorded or recorded == want:
        log(f"sysroot 未漂移: {want}")
        return
    if os.environ.get("ALLOW_SDK_SWITCH"):
        warn(f"ALLOW_SDK_SWITCH=1 —— 放行 SDK 切换 {recorded} -> {want}（本次将全量重编）")
        return
    err(f"sysroot 漂移，继续跑会触发全量重编:\n"
        f"  构建图已记录: {recorded}\n  本次将使用  : {want}\n"
        f"  三选一: (a) 回到 {recorded} 所在的 Xcode（DEVELOPER_DIR=/Applications/Xcode-<ver>.app/Contents/Developer）\n"
        f"          (b) sudo xcode-select -s <该 Xcode>/Contents/Developer\n"
        f"          (c) 确认接受全量重编: ALLOW_SDK_SWITCH=1 ...")


def xcode_prepare(c: Ctx):
    """Chromium 树与 SDK 版本强绑定（换一个数小时全量重编）：并存安装时优先
    Xcode-26.5*（本树配套 MacOSX26.5 SDK），可由 .env chromium_developer_dir 覆盖；
    外部已 export 的 DEVELOPER_DIR 最优先。"""
    if HOST_OS != "mac":
        return
    apply_developer_dir(c.cfg, "chromium_developer_dir", "developer_dir")
    if not os.environ.get("DEVELOPER_DIR"):
        for xc in sorted(Path("/Applications").glob("Xcode-26.5*.app/Contents/Developer")):
            if xc.is_dir():
                os.environ["DEVELOPER_DIR"] = str(xc)
                log(f"DEVELOPER_DIR（Chromium 配套 Xcode）: {xc}")
                break
    dev = out(["xcode-select", "-p"])
    if not dev or "Xcode" not in dev:
        warn("xcode-select 未指向完整 Xcode: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer")
    if not out(["xcrun", "clang", "--version"]):
        warn("xcrun clang 不可用 —— 可能未接受许可: sudo xcodebuild -license accept")


def do_gen(c: Ctx):
    ensure_depot_tools(c.cfg)
    xcode_prepare(c)
    _mac_sdk_guard(c)

    comp = "false" if c.link == "static" else "true"
    args = [("is_debug", "false"),
            ("symbol_level", "0"),
            ("is_component_build", comp),
            ("target_cpu", f'"{c.arch}"'),
            ("use_siso", "false")]
    if cget(c.cfg, "codecs", "CODECS"):
        # 必须成对：proprietary_codecs + ffmpeg_branding="Chrome"
        # （写 use_proprietary_codecs 是无效 arg，gn 静默忽略后三项能力全缺）
        args += [("proprietary_codecs", "true"), ("ffmpeg_branding", '"Chrome"')]
    if cget(c.cfg, "official", "OFFICIAL"):
        args += [("is_official_build", "true")]
    if c.os == "win":
        args += [("enable_nacl", "false")]

    write_args_gn(c.out_dir / "args.gn",
                  f"# Chromium baseline — {c.os} {c.arch} {c.link}", args, extra_gn_args(c))
    gn_gen(c, c.out_dir / "args.gn")
    log(f"gn gen 完成: {c.out_dir}")


def do_build(c: Ctx):
    ensure_depot_tools(c.cfg)
    _mac_sdk_guard(c)
    if not (c.out_dir / "build.ninja").exists():
        warn("尚未 gn gen —— 顺手补一次")
        do_gen(c)
    from .common import build_cmd, probe_steps
    steps = probe_steps(c, [TARGET])
    if steps == 0:
        log("所有目标均无工作（ninja: no work to do）—— 跳过编译")
    else:
        if steps and steps >= 5000:
            warn(f"本次要编 {steps} 步 —— 不像一次小改动，多半是级联（sysroot/args.gn/版本变了）")
        if not run(build_cmd(c, [TARGET]), cwd=SRC):
            err(f"编译失败: {TARGET}")
    log("编译成功")

    app = {"mac": c.out_dir / "Chromium.app",
           "win": c.out_dir / "chrome.exe",
           "linux": c.out_dir / "chrome"}[c.os]
    if app.exists():
        log(f"产物: {app} ({human_size(app)})")
    else:
        warn(f"未找到产物: {app}")


ACTIONS = ("down", "gen", "build")


def run_chromium(c: Ctx, actions: list):
    for a in actions:
        if a == "down":
            do_down(c)
        elif a == "gen":
            do_gen(c)
        elif a == "build":
            do_build(c)
