#!/usr/bin/env python3
# =============================================================================
#  工作区编译 / 测试 / 打包统一入口（Windows / macOS / Linux）
#
#  用法: python3 scripts/build.py <项目> [动作...] [选项]
#    项目（必选其一）:
#      chromium   Chromium 官方基线（对照用，不带内核）
#      kernel     nomad 内核（编进 Chromium 树的那个模块）
#      browser    nomad 浏览器（PC 外壳，消费内核交付包）
#      all        kernel -> browser（完整交付链路）
#      print-delivery  打印当前已验收的内核交付包根（给上层脚本/PC 编译用）
#    动作（可多选，按顺序执行；默认 build）:
#      down     拉齐依赖与工具链（gclient sync + runhooks）
#      gen      gn gen（写 args.gn + 生成构建图）
#      build    编译
#      test     编译并运行测试目标（仅 kernel）
#      package  出交付包到 dist/（kernel: 仅 static 允许）
#      all      down + gen + build（+ kernel: test/package 需显式指定）
#    选项:
#      --os win|mac|linux|android      目标系统（默认宿主）
#      --arch x86|x64|arm64|apple      目标架构（默认宿主；apple=arm64）
#      --ver X.Y.Z.W                   版本号（默认取 src/chrome/VERSION）
#      --link static|dynamic           静态/组件构建（默认: kernel/browser=static, chromium=dynamic）
#      --jobs N                        并行度（默认 autoninja 自行决定）
#      --delivery <路径>               显式指定内核交付包根（browser 用）
#      --delivery-n N                  指定交付次数（默认自动递增）
#      --keep-history                  打包时保留历史交付包
#      --no-link                       不动 src/chrome/browser/arupa_desktop 挂载
#      --no-gate                       跳过交付前门禁（产物新鲜度等）
#      --no-web                        browser 出包时跳过 WebUI 构建
#      -y, --yes                       非交互（当前无交互确认，保留兼容）
#      --dry-run                       只打印命令，不执行
#      -h, --help
#
#  目录规范:
#    编译  out/<project>-<os>-<arch>-<ver>-<static|dynamic>
#    交付  dist/<project>-<os>-<arch>-<ver>-<n>
#
#  例:
#    python3 scripts/build.py kernel gen build --arch arm64 --link static
#    python3 scripts/build.py kernel package            # 出交付包（自动递增编号）
#    python3 scripts/build.py browser package           # PC 出包（自动取最新内核交付包）
#    python3 scripts/build.py chromium all --link dynamic
# =============================================================================
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import builder.common as C  # noqa: E402
from builder.common import (ARCHS, Ctx, apply_proxy, chromium_version, check_project,
                            err, log, normalize_arch, normalize_link, normalize_project,
                            step, warn, host_arch, HOST_OS, OUT_ROOT, DIST_ROOT)  # noqa: E402
import builder.chromium as chromium  # noqa: E402
import builder.kernel as kernel  # noqa: E402
import builder.browser as browser  # noqa: E402

ACTION_ALIASES = {"zip": "package", "pack": "package", "sync": "down", "compile": "build"}
ACTIONS = ("down", "gen", "build", "test", "package")
DEFAULT_LINK = {"chromium": "dynamic", "kernel": "static", "browser": "static"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build.py", description="工作区编译 / 测试 / 打包（三平台统一入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:\n"
               "  python3 scripts/build.py kernel gen build --arch arm64 --link static\n"
               "  python3 scripts/build.py kernel package\n"
               "  python3 scripts/build.py browser package\n"
               "  python3 scripts/build.py chromium all --link dynamic\n")
    p.add_argument("project", metavar="项目",
                   help="chromium / kernel / browser / all / print-delivery")
    p.add_argument("actions", nargs="*", metavar="动作", help=" / ".join(ACTIONS) + "（默认 build）")
    p.add_argument("--os", dest="os_name", default="", help="win / mac / linux / android")
    p.add_argument("--arch", default="", help="x86 / x64 / arm64（apple = arm64）")
    p.add_argument("--ver", default="", help="版本号（默认取自 src/chrome/VERSION）")
    p.add_argument("--link", default="", help="static / dynamic")
    p.add_argument("--jobs", type=int, default=0, help="并行度")
    p.add_argument("--delivery", default="", help="显式指定内核交付包根（browser）")
    p.add_argument("--delivery-n", dest="delivery_n", type=int, default=0, help="指定交付次数")
    p.add_argument("--keep-history", action="store_true", help="打包时保留历史交付包")
    p.add_argument("--no-link", action="store_true", help="不动挂载点")
    p.add_argument("--no-gate", action="store_true", help="跳过交付前门禁")
    p.add_argument("--no-web", action="store_true", help="browser 出包时跳过 WebUI")
    p.add_argument("--proxy", default="", help="HTTP 代理（为空=不用代理）")
    p.add_argument("-y", "--yes", action="store_true", help="非交互")
    p.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    C.DRY_RUN = args.dry_run
    C.YES = args.yes
    cfg = C.load_config()
    apply_proxy(cfg, args.proxy)

    proj_raw = args.project.lower()
    if proj_raw in ("print-delivery", "print_delivery", "delivery"):
        c = make_ctx(cfg, "kernel", args)
        return kernel.print_delivery(c)

    proj = normalize_project(proj_raw)
    if proj not in ("chromium", "kernel", "browser", "all"):
        err(f"未知项目: {args.project}（可选: chromium / kernel / browser / all / print-delivery）")

    actions = [ACTION_ALIASES.get(a.lower(), a.lower()) for a in args.actions] or ["build"]
    if "all" in actions:
        i = actions.index("all")
        actions[i:i + 1] = ["down", "gen", "build"]
    unknown = [a for a in actions if a not in ACTIONS]
    if unknown:
        err("未知动作: " + " ".join(unknown) + "（可选: " + " / ".join(ACTIONS) + " / all）")

    projects = [proj] if proj != "all" else ["kernel", "browser"]
    log(f"工作区: {C.WORKSPACE_ROOT}")
    for p in projects:
        if p == "chromium" and any(a in ("test", "package") for a in actions):
            warn(f"chromium 基线不支持 {'/'.join(a for a in actions if a in ('test', 'package'))}，跳过")
        c = make_ctx(cfg, p, args)
        step(f"{p} [{c.os}/{c.arch}/{c.link}] {c.ver}")
        log(f"编译目录: {c.out_dir}")
        acts = [a for a in actions if not (p == "chromium" and a in ("test", "package"))]
        if p == "chromium":
            chromium.run_chromium(c, [a for a in acts if a in chromium.ACTIONS])
        elif p == "kernel":
            kernel.run_kernel(c, [a for a in acts if a in kernel.ACTIONS])
        else:
            browser.run_browser(c, [a for a in acts if a in browser.ACTIONS])

    step("完成")
    return 0


def make_ctx(cfg: dict, project: str, args) -> Ctx:
    os_name = (args.os_name or os.environ.get("TARGET_OS") or
               cfg.get("target_os") or HOST_OS).lower()
    arch = normalize_arch(args.arch or os.environ.get("TARGET_ARCH") or
                          cfg.get("target_arch") or host_arch())
    check_project(project, os_name, arch)
    ver = args.ver or os.environ.get("CHROMIUM_VERSION") or chromium_version(cfg)
    link = normalize_link(args.link or os.environ.get("LINK_MODE") or
                          cfg.get("link_mode") or DEFAULT_LINK[project])
    if link not in ("static", "dynamic"):
        err(f"未知链接模式: {link}（static / dynamic）")
    return Ctx(project=project, os=os_name, arch=arch, ver=ver, link=link,
               jobs=args.jobs or int(cfg.get("jobs", "0") or 0), cfg=cfg,
               no_link=args.no_link, keep_history=args.keep_history,
               delivery_no=args.delivery_n, gate=not args.no_gate,
               webui=not args.no_web, delivery_root=args.delivery)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("已中断")
