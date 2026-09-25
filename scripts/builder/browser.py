#!/usr/bin/env python3
# =============================================================================
#  nomad 浏览器（PC 外壳）编译 / 出包
#
#  编译目录: out/browser-<os>-<arch>-<ver>-<static|dynamic>   （dotnet 中间/落盘产物）
#  交付目录: dist/browser-<os>-<arch>-<ver>-<n>
#
#  前置: 内核交付包必须先存在并通过门禁（python3 scripts/build.py kernel package）。
#  交付根解析优先级: --delivery / NOMAD_ARUPA_DELIVERY_ROOT  → dist/.delivery-build
#                    → dist/ 里同平台最新编号。解析失败在任何副作用之前退出。
# =============================================================================
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .common import (PC_REPO, DIST_ROOT, Ctx, apply_developer_dir, cget, err, log,
                     out, run, warn, human_size, next_delivery_no,
                     purge_previous_deliveries, record_delivery)
from . import common

RIDS = {
    "win": {"x64": "win-x64", "x86": "win-x86", "arm64": "win-arm64"},
    "mac": {"x64": "osx-x64", "arm64": "osx-arm64"},
    "linux": {"x64": "linux-x64", "arm64": "linux-arm64"}
}


def rid(c: Ctx) -> str:
    return RIDS.get(c.os, {}).get(c.arch, f"{c.os}-{c.arch}")


def resolve_delivery(c: Ctx) -> Path:
    env = c.delivery_root or os.environ.get("NOMAD_ARUPA_DELIVERY_ROOT", "")
    if env:
        p = Path(env).expanduser()
        if not p.is_dir():
            err(f"指定的交付根不存在: {p}")
        log(f"交付根（显式指定）: {p}")
        return p
    f = DIST_ROOT / ".delivery-build"
    if f.exists():
        name = f.read_text(encoding="utf-8").strip()
        p = DIST_ROOT / name
        if p.is_dir():
            log(f"交付根（dist/.delivery-build）: {p}")
            return p
    pat = re.compile(r"^kernel-" + re.escape(f"{c.os}-{c.arch}") + r"-[\d.]+-(\d+)$")
    best = None
    if DIST_ROOT.is_dir():
        for p in DIST_ROOT.iterdir():
            m = pat.match(p.name)
            if m and p.is_dir():
                best = max(best or (0, p), (int(m.group(1)), p))
    if not best:
        err("找不到内核交付包 —— 先跑: python3 scripts/build.py kernel package\n"
            f"  期望形如: {DIST_ROOT}/kernel-{c.os}-{c.arch}-<ver>-<n>")
    log(f"交付根（同平台最新编号）: {best[1]}")
    return best[1]


def dotnet() -> str:
    d = shutil.which("dotnet")
    if not d:
        for c in ("/usr/share/dotnet/dotnet", "/usr/lib/dotnet/dotnet",
                  "/usr/local/share/dotnet/dotnet"):
            if Path(c).exists():
                return c
        err("未找到 dotnet —— 需要 .NET 10 SDK")
    return d


def project_paths(c: Ctx):
    """按 OS 选主工程；PC 仓没有该平台工程时明确报错（不猜）。"""
    if not PC_REPO.is_dir():
        err(f"PC 仓不存在: {PC_REPO}")
    if c.os == "mac":
        mac_proj = PC_REPO / "NomadBrowser.Avalonia.Mac" / "NomadBrowser.Avalonia.Mac.csproj"
        sln = PC_REPO / "NomadBrowser.Mac.slnx"
        if not mac_proj.exists():
            err(f"缺 Mac 主工程: {mac_proj}")
        return sln if sln.exists() else mac_proj, mac_proj
    if c.os == "linux":
        for cand in sorted((PC_REPO / "NomadBrowser.Avalonia.Linux").glob("*.csproj")):
            return PC_REPO / "NomadBrowser.Linux.slnx", cand
        proj = PC_REPO / "NomadBrowser.Avalonia" / "NomadBrowser.Avalonia.csproj"
        if not proj.exists():
            err("找不到 Linux 主工程（期望 NomadBrowser.Avalonia.Linux/*.csproj 或 "
                "NomadBrowser.Avalonia/NomadBrowser.Avalonia.csproj）")
        return PC_REPO / "NomadBrowser.sln", proj
    proj = PC_REPO / "NomadBrowser.Avalonia" / "NomadBrowser.Avalonia.csproj"
    if not proj.exists():
        err(f"缺 Windows 主工程: {proj}")
    return proj, proj


def ensure_node_modules(web: Path):
    """node_modules 不存在时先把前端依赖装上 —— 否则 prebuild 里的 i18n 校验脚本
    会以 'Cannot find module esbuild' 挂掉，看着像源码坏了，其实只是没装依赖。"""
    if (web / "node_modules").is_dir():
        return
    lock = web / "package-lock.json"
    log("首次构建 WebUI —— 安装前端依赖（node_modules 不存在）")
    if lock.exists() and run(["npm", "ci"], cwd=web, check=False):
        return
    run(["npm", "install"], cwd=web)


def build_webui(c: Ctx):
    """WebUI 是 .app 里的 Resources；不编会用上一份（调试快，交付别用）。"""
    if not c.webui:
        log("--no-web：跳过 WebUI 构建，复用已有 Resources")
        return
    web = PC_REPO / "NomadWebUI" / "nomadwebui"
    if not (web / "package.json").exists():
        warn(f"未找到 WebUI 工程: {web}")
        return
    if not shutil.which("npm"):
        warn("未找到 npm —— 跳过 WebUI 构建")
        return
    ensure_node_modules(web)
    if run(["npm", "run", "build"], cwd=web, check=False):
        return
    # IDE / 沙箱注入的 NODE_OPTIONS --require shim 会拦住 vite 清空 outDir 的 rmSync，
    # 失败信息看着像前端坏了，实际是外层拦截 —— 去掉注入重试一次，并把原因说清楚。
    if os.environ.pop("NODE_OPTIONS", None) is not None:
        warn("npm run build 失败：检测到 NODE_OPTIONS 注入（外层 node shim 会拦 vite 清 outDir）—— 去掉后重试")
        run(["npm", "run", "build"], cwd=web)
        return
    err("WebUI 构建失败（npm run build）—— 交付前需要它产出 Resources；只编 C# 侧请加 --no-web")


def do_build(c: Ctx):
    apply_developer_dir(c.cfg, "pc_developer_dir", "developer_dir")
    sln, main_proj = project_paths(c)
    cfg = cget(c.cfg, "pc_config", "PC_CONFIG", default="Release")
    env_api = os.environ.get("NOMAD_API_ENV", "Production")
    delivery = resolve_delivery(c)

    dn = dotnet()
    run([dn, "restore", str(sln)], cwd=PC_REPO)
    if c.os == "mac":
        # -p:BuildMac=true 必须显式给：slnx 直含的 Core/PluginContracts 等工程
        # 与 Avalonia.Mac 引用链全局属性不一致时，MSBuild 会把同一工程编两次。
        run([dn, "build", str(sln), "-c", cfg, "--no-restore",
             "-p:BuildMac=true", f"-p:ArupaDeliveryRoot={delivery}"], cwd=PC_REPO)
    else:
        run([dn, "build", str(main_proj), "-c", cfg, "--no-restore",
             f"-p:ArupaDeliveryRoot={delivery}"], cwd=PC_REPO)
    log("PC 编译完成")


def do_package(c: Ctx):
    apply_developer_dir(c.cfg, "pc_developer_dir", "developer_dir")
    sln, main_proj = project_paths(c)
    cfg = cget(c.cfg, "pc_config", "PC_CONFIG", default="Release")
    env_api = os.environ.get("NOMAD_API_ENV", "Production")
    delivery = resolve_delivery(c)
    dn = dotnet()
    build_webui(c)

    if common.DRY_RUN:
        log(f"(dry-run) 将出包: 项目 {main_proj.name} / RID {rid(c)} / 交付根 {delivery}")
        return None
    c.out_dir.mkdir(parents=True, exist_ok=True)
    payload = c.out_dir / "payload"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)

    if c.os == "mac":
        # 必须是 publish 而不是 build：mac-bundle.targets 里 BuildAppBundle 挂在
        # AfterTargets="Publish" 上（-t:BuildAppBundle 也可），光 build 只会得到中间产物，
        # 最后 dist/macos/ 下不会有 .app。
        # RID 只在工程级传（NETSDK1134 禁止解决方案级 RID）；MacRuntimeIdentifier 默认跟随 -r。
        # 中间产物默认落在 $(HOME)/Desktop/dist —— Desktop 受 TCC 保护，文件会被贴
        # com.apple.provenance，随后 codesign 间歇性 Operation not permitted（xattr -cr 也清不掉）。
        # 改指到工作区自己的 out/ 下（本地普通卷且已被 .gitignore 覆盖），并清掉上次的发布
        # 暂存，避免把还带着 provenance 的旧件再拷进 bundle。
        local_out = c.out_dir / "dotnet-local"
        local_out.mkdir(parents=True, exist_ok=True)
        for stale in ("main-publish", "kernelhost-publish", "mac-updater"):
            shutil.rmtree(PC_REPO / "dist" / "macos" / stale, ignore_errors=True)
        run([dn, "publish", str(main_proj), "-c", cfg, "-r", rid(c),
             "-p:BuildMac=true", f"-p:MacConfiguration={cfg}",
             f"-p:LocalDebugOutputRoot={local_out}",
             f"-p:NomadApiEnvironment={env_api}", f"-p:ArupaDeliveryRoot={delivery}"],
            cwd=PC_REPO)
        # mac-bundle-props.props: MacDistRoot = <repo>/dist/macos（x64 时再套一层 osx-x64）
        app = PC_REPO / "dist" / "macos" / "逐风浏览器.app"
        if not app.is_dir() and c.arch == "x64":
            app = PC_REPO / "dist" / "macos" / "osx-x64" / "逐风浏览器.app"
        if not app.is_dir():
            err(f"未找到 .app: {app}（Mac 目标没产出 bundle）")
    elif c.os == "linux":
        run([dn, "publish", str(main_proj), "-c", cfg, "-r", rid(c),
             "--no-restore", "-o", str(payload), f"-p:ArupaDeliveryRoot={delivery}"],
            cwd=PC_REPO)
    else:
        run([dn, "publish", str(main_proj), "-c", cfg, "-r", rid(c),
             "--self-contained", "true", "--no-restore", "-o", str(payload),
             "-p:Platform=x64", f"-p:ArupaDeliveryRoot={delivery}"], cwd=PC_REPO)

    # 组装交付目录
    n = next_delivery_no(c)
    name = c.dist_name(n)
    stage = DIST_ROOT / name
    if stage.exists():
        err(f"交付目录已存在，拒绝覆盖: {stage}")
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True)

    if c.os == "mac":
        dst = stage / app.name
        shutil.copytree(app, dst, symlinks=True)
    else:
        dst = stage / ("NomadBrowser" if c.os == "linux" else "payload")
        shutil.copytree(payload, dst, symlinks=True)
        if c.os == "linux":
            # Linux 没有 bundle 概念：便携目录 = 主程序 + kernel/ + Resources/ + run.sh
            shutil.copytree(delivery / "kernel", stage / "kernel", dirs_exist_ok=True)
            res = PC_REPO / "dist" / "Debug" / "Resources"
            if res.is_dir():
                shutil.copytree(res, dst / "Resources", dirs_exist_ok=True)
            (stage / "run.sh").write_text(
                "#!/usr/bin/env bash\ncd \"$(dirname \"$0\")/NomadBrowser\"\n"
                "export LD_LIBRARY_PATH=\"$(dirname \"$0\")/kernel:$LD_LIBRARY_PATH\"\n"
                "exec ./NomadBrowser \"$@\"\n", encoding="utf-8")
            (stage / "run.sh").chmod(0o755)

    # 内核件必须真的进了载荷，否则这包装起来也跑不起来
    marks = {"mac": [dst / "Contents" / "Resources" / "arupa-mac"],
             "linux": [stage / "kernel"],
             "win": [dst / "NomadBrowser.exe"]}[c.os]
    missing = [str(m) for m in marks if not m.exists()]
    if missing:
        err("载荷缺必需件: " + ", ".join(missing))

    purge_previous_deliveries(c, keep=name)
    record_delivery(c, n, stage, sorted(p.relative_to(stage).as_posix()
                                        for p in stage.rglob("*") if p.is_file()))
    (DIST_ROOT / ".delivery-build-browser").write_text(name + "\n", encoding="utf-8")
    log(f"交付目录: {stage} ({human_size(stage)})")
    return stage


ACTIONS = ("build", "package")


def run_browser(c: Ctx, actions: list):
    for a in actions:
        if a == "build":
            do_build(c)
        elif a == "package":
            do_package(c)
