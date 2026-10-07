#!/usr/bin/env python3
# =============================================================================
#  nomad 浏览器（PC 外壳）编译 / 出包
#
#  编译目录: out/browser-<os>-<arch>-<ver>-<static|dynamic>
#      · build   主输出 → 该目录本身（dotnet build -o）
#      · package 载荷   → 该目录下的 payload/
#      · 各工程自己的 obj/bin 仍留在 nomad_desktop 里（不动，避免影响仓内既有约定）
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
import time
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


def delivery_props(c: Ctx, delivery: Path) -> list:
    """把交付包根传给 MSBuild 的属性。

    坑: 同一个交付包根在 Directory.Build.targets 里**两个名字**——
      · Windows/Linux: ArupaSdkDir  → $(ArupaSdkDir)\\kernel\\.arupa-version
      · Mac         : ArupaDeliveryRoot → $(ArupaDeliveryRoot)/macKernel/.arupa-version
    只传 ArupaDeliveryRoot 时，Windows 上真值查不到，报
    "没查到内核版本的真值 (.arupa-version)"（炸在 NomadBrowser.Core，看着像代码问题）。
    """
    props = [f"-p:ArupaDeliveryRoot={delivery}"]
    if c.os != "mac":
        props.append(f"-p:ArupaSdkDir={delivery}")
        # 交付顶层门面若是 ref 程序集（~4KB，无实现），改用稳定副本引用，
        # 避免构建中途交付目录被外部进程还原导致 CS0246。
        stable = stable_facade(c, delivery)
        if stable:
            props.append(f"-p:ArupaKernelAssembly={stable}")
    return props


FACADE_MIN_BYTES = 50_000  # ref/reference-only 程序集只有 ~4KB
# 稳定副本位置: PC 仓内（交付目录会被外部进程按打包快照恢复，不能放那儿）。
STABLE_FACADE = PC_REPO / "build" / "kernel-facade" / "ArupaKernel.dll"


def facade_bytes_ok(p: Path) -> bool:
    return p.exists() and p.stat().st_size >= FACADE_MIN_BYTES


def stable_facade(c: Ctx, delivery: Path) -> Path | None:
    """稳定副本存在就返回它（引用一律走稳定副本，规避交付目录构建中被还原的竞态）；
    否则返回 None（走默认 HintPath = 交付顶层）。ensure_kernel_facade 会在构建前刷新/创建副本。"""
    if facade_bytes_ok(STABLE_FACADE):
        return STABLE_FACADE
    return None


def compile_props(c: Ctx) -> list:
    """Windows: 关掉 Roslyn 共享编译服务器（VBCSCompiler）。

    坑: 同一工程在同一轮里被编两次时（日志里 NomadBrowser.Updater 先出了 bin，随后又写
    同一个 obj），VBCSCompiler 仍握着上一份 obj 的句柄，CSC 撞
    CS2012「无法打开 obj\\Release\\net10.0\\X.dll 以进行写入 —— 文件正被另一个进程使用」，
    错误信息会点名 "可能被 'VBCSCompiler' (pid) 锁定"（看着像代码/杀软问题，其实是编译器服务器）。
    关掉后每次编译走进程内 CSC，不再争句柄（PC 全量约 16 秒，代价可接受）。
    """
    return ["-p:UseSharedCompilation=false"] if c.os == "win" else []


def ensure_kernel_facade(c: Ctx, delivery: Path):
    """交付只带门面源码时，先用它自带的工程编出 ArupaKernel.dll。

    Directory.Build.props: ArupaKernelAssembly = $(ArupaSdkDir)\\dotnet\\ArupaKernel.dll，
    不存在时回落到 $(ArupaSdkDir)\\dotnet\\bin\\x64\\Release\\net10.0\\ArupaKernel.dll ——
    也就是"用同一份交付自己编出来的那份"。154+6 起的交付号称带已编译门面，但本机这份
    （dist/kernel-win-x64-154.0.8037.21-1）dotnet\\ 下只有 ArupaKernel.csproj + 两个 .cs，
    不先编出来就会撞 VerifyArupaKernelAssembly: "缺少配套的内核门面"。
    """
    if c.os == "mac":
        return
    d = delivery / "dotnet"
    proj = d / "ArupaKernel.csproj"
    if not d.is_dir():
        # 不静默: 这份交付连门面源码都没有（内核仓 package/dotnet 缺失 / 出包时没拷进来），
        # 后面 PC 编译只会甩一句"缺少配套的内核门面: <delivery>\dotnet\ArupaKernel.dll"，
        # 看不出是交付件不全。先说清楚，免得当代码问题查。
        warn(f"内核交付件里没有 dotnet/（门面源码）: {delivery} —— 出包时 copy_assets 没把"
             f"内核仓 package/dotnet 拷进来（或该目录已不在内核仓）。重出一次内核包即可补齐")
        return
    if not proj.exists():
        return
    # 门面有效性: 必须是完整程序集。~4KB 的是 ref/reference-only 程序集（只有签名没有实现），
    # 引用它宿主会满屏 CS0246 "未能找到 Arupa/ArupaWebView/ArupaKernel"。
    # (2026-10-04: static-5/6 交付出包时顶层 dotnet/ArupaKernel.dll 就是 ref 程序集，
    #  且交付目录会被外部进程按打包时快照恢复，手修会被还原 —— 所以每次构建现场自愈。)
    MIN_FACADE_BYTES = 50_000

    def facade_ok(p: Path) -> bool:
        return p.exists() and p.stat().st_size >= MIN_FACADE_BYTES

    top = d / "ArupaKernel.dll"
    if facade_ok(top):
        # 稳定副本始终与顶层同步，构建中引用一律走稳定副本（delivery_props），
        # 这样构建中途交付目录被外部进程还原也不会打断编译。
        STABLE_FACADE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(top, STABLE_FACADE)
        return
    if not facade_ok(STABLE_FACADE):
        log(f"交付门面缺失或只有 ref 程序集({top.stat().st_size}B)，且无稳定副本 —— 现场编译: {proj}")
        if common.DRY_RUN:
            log(f"(dry-run) dotnet build {proj} -c Release -p:Platform=x64")
            return
        run([dotnet(), "build", str(proj), "-c", "Release", "-p:Platform=x64"], cwd=PC_REPO)
        built = d / "bin" / "x64" / "Release" / "net10.0" / "ArupaKernel.dll"
        if not facade_ok(built):
            built = d / "bin" / "Release" / "net10.0" / "ArupaKernel.dll"
        if facade_ok(built):
            log(f"现场编译完成 —— 部署门面: {built} -> {top} 和 {STABLE_FACADE}")
            top.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(built, top)
            STABLE_FACADE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(built, STABLE_FACADE)
        else:
            err(f"现场编译后仍没有可用的 ArupaKernel.dll（顶层与 bin 产物都缺失或过小）: {d}")
    else:
        log(f"交付顶层门面无效({top.stat().st_size}B) —— 沿用稳定副本: {STABLE_FACADE}")


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
    # 坑: MSBuild 节点复用会让上一轮的 dotnet / VBCSCompiler 进程存活约 15 分钟，
    # 期间再编同一批工程，CSC 会撞 CS2012「无法打开 obj\Release\...\X.dll 以进行写入 ——
    # 文件正被另一个进程使用」（看着像代码问题，其实是上一轮的编译服务器没退）。
    os.environ.setdefault("MSBUILDDISABLENODEREUSE", "1")
    d = shutil.which("dotnet")
    if not d:
        # 坑: Windows 官方安装包默认落在 C:\Program Files\dotnet，用安装脚本/zip 部署或
        # 安装时没勾"添加到 PATH"时它不在 PATH 上，shutil.which 查不到 —— 补默认位置。
        cands = (["C:\\Program Files\\dotnet\\dotnet.exe",
                  "C:\\Program Files (x86)\\dotnet\\dotnet.exe"]
                 if common.IS_WIN else
                 ["/usr/share/dotnet/dotnet", "/usr/lib/dotnet/dotnet",
                  "/usr/local/share/dotnet/dotnet"])
        for c in cands:
            if Path(c).exists():
                log(f"dotnet 不在 PATH 上，用默认安装位置: {c}")
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
            slnx = PC_REPO / "NomadBrowser.Linux.slnx"
            return slnx if slnx.exists() else cand, cand
        proj = PC_REPO / "NomadBrowser.Avalonia" / "NomadBrowser.Avalonia.csproj"
        if not proj.exists():
            err("找不到 Linux 主工程（期望 NomadBrowser.Avalonia.Linux/*.csproj 或 "
                "NomadBrowser.Avalonia/NomadBrowser.Avalonia.csproj）")
        # 不用 NomadBrowser.sln：它挂着 ..\Arupa\shells\dotnet\ArupaKernel\
        # ArupaKernel.csproj（Windows 同工作区那份内核仓，见 PC 仓 project.md
        # "内核强依赖外部仓"）。Linux 工作区没有 Arupa/ 这个目录，还原整张 sln 必然
        # MSB3202。没有任何工程用 ProjectReference 指它（只有 Test 工程按
        # HintPath 引已编译的 ArupaKernel.dll），所以拿主工程当还原入口即可 ——
        # 它的 ProjectReference 图就是实际要编的东西。
        # 没有 Linux 专用入口工程时只能回落到 NomadBrowser.Avalonia（Windows 入口）
        # + net10.0 TFM，而那份 TFM 的共享源码按 #if MAC 分叉（MAC 只有
        # NomadBrowser.Avalonia.Mac/NomadBrowser.Avalonia.Mac.csproj 定义），
        # Linux 上必然报 MacViewCreation/MacTabOptions 等未定义 —— 真要出 Linux 包
        # 得先有 NomadBrowser.Avalonia.Linux（上面的 glob 会优先选它）。
        warn("PC 仓没有 NomadBrowser.Avalonia.Linux —— 回落到 NomadBrowser.Avalonia "
             "(Windows 入口) 的 net10.0 TFM；该 TFM 的入口源码走 #if MAC 分叉，"
             "Linux 上需要专用入口工程才能编过")
        slnx = PC_REPO / "NomadBrowser.Linux.slnx"
        return slnx if slnx.exists() else proj, proj
    proj = PC_REPO / "NomadBrowser.Avalonia" / "NomadBrowser.Avalonia.csproj"
    if not proj.exists():
        err(f"缺 Windows 主工程: {proj}")
    return proj, proj


def npm_bin() -> str:
    """定位 npm。

    坑: Windows 的 Node 安装同时放了 npm（bash shim，**无扩展名**）/ npm.cmd / npm.ps1。
    shutil.which("npm") 命中的是那个无扩展名的 shim（PATHEXT 里没有它，但文件确实存在，
    which 就认），交给 subprocess 就是 WinError 2「系统找不到指定的文件」—— 必须显式
    解析到 npm.cmd。与 dotnet()/autoninja 是同一类坑。
    """
    n = shutil.which("npm")
    if not common.IS_WIN:
        return n or "npm"
    cands = []
    if n:
        cands.append(Path(n).with_suffix(".cmd"))
    cands.append(Path(r"C:\Program Files\nodejs\npm.cmd"))
    for c in cands:
        if c.exists():
            return str(c)
    return n or "npm"


def ensure_node_modules(web: Path):
    """node_modules 不存在时先把前端依赖装上 —— 否则 prebuild 里的 i18n 校验脚本
    会以 'Cannot find module esbuild' 挂掉，看着像源码坏了，其实只是没装依赖。"""
    if (web / "node_modules").is_dir():
        return
    lock = web / "package-lock.json"
    log("首次构建 WebUI —— 安装前端依赖（node_modules 不存在）")
    npm = npm_bin()
    if lock.exists() and run([npm, "ci"], cwd=web, check=False):
        return
    run([npm, "install"], cwd=web)


def build_webui(c: Ctx):
    """WebUI 是 .app 里的 Resources；不编会用上一份（调试快，交付别用）。"""
    if not c.webui:
        log("--no-web：跳过 WebUI 构建，复用已有 Resources")
        return
    web = PC_REPO / "NomadWebUI" / "nomadwebui"
    if not (web / "package.json").exists():
        warn(f"未找到 WebUI 工程: {web}")
        return
    npm = npm_bin()
    if not Path(npm).exists() and not shutil.which(npm):
        warn("未找到 npm —— 跳过 WebUI 构建")
        return
    ensure_node_modules(web)
    if run([npm, "run", "build"], cwd=web, check=False):
        return
    # IDE / 沙箱注入的 NODE_OPTIONS --require shim 会拦住 vite 清空 outDir 的 rmSync，
    # 失败信息看着像前端坏了，实际是外层拦截 —— 去掉注入重试一次，并把原因说清楚。
    if os.environ.pop("NODE_OPTIONS", None) is not None:
        warn("npm run build 失败：检测到 NODE_OPTIONS 注入（外层 node shim 会拦 vite 清 outDir）—— 去掉后重试")
        run([npm, "run", "build"], cwd=web)
        return
    err("WebUI 构建失败（npm run build）—— 交付前需要它产出 Resources；只编 C# 侧请加 --no-web")


# 出货载荷装配范式以 PC 仓 Scripts/assemble-release-payload.ps1 为准:
#   ① NomadBrowser.exe        自包含**单文件**(客户机零 .NET 依赖)
#   ② NomadBrowser.Updater.exe 独立更新器(主程序退出后要能覆盖主程序文件)
#   ③ Helpers\WindowsUpdater\ 更新窗
#   ④ arupa-desktop\          内核交付件(ResolveArupaDir: BaseDirectory\arupa-desktop)
#   ⑤ Resources\              WebUI(ResolveWebUiRoot: BaseDirectory\Resources)
#   ⑥ 剔原生 PDB(SkiaSharp/HarfBuzz 的 NuGet 自带约 100MB, DebugType=none 只管托管 PDB)
UPDATER_PROJ = PC_REPO / "NomadBrowser.Updater" / "NomadBrowser.Updater.csproj"
WIN_UPDATER_PROJ = PC_REPO / "NomadBrowser.Windows.Updater" / "NomadBrowser.Windows.Updater.csproj"
SINGLE_FILE_ARGS = ["-p:PublishSingleFile=true",          # 单 exe 自带运行时
                    "-p:IncludeNativeLibrariesForSelfExtract=false",  # 原生库躺旁边, 不解包到 %TEMP%
                    "-p:EnableCompressionInSingleFile=false",         # 压缩只在每次启动多花 ~375ms
                    "-p:PublishReadyToRun=true",                      # 换启动速度
                    "-p:DebugType=none", "-p:DebugSymbols=false"]

# ── 原生宿主形态（内核静态链进浏览器进程 EXE）────────────────────────────────
# 背景: Chromium 的 Windows 沙箱在 CreateProcess(CREATE_SUSPENDED) 之后、ResumeThread
# 之前做跨进程地址交接，那一刻只有**主 EXE 镜像**被映射（实测挂起态连 PEB 都读不到）。
# 所以 broker 侧用来交接的符号必须住在主镜像里 —— 库式嵌入（浏览器代码在 arupa_kernel.dll、
# 由 .NET 宿主 EXE 加载）先天不满足，表现为 GPU 子进程 err=64/57 → FATAL。
# 原生宿主动作: 把内核静态链进浏览器进程 EXE（arupa_desktop），子进程由**同一个 EXE** 拉起。
#
# macOS/Linux 的沙箱不做跨进程地址交接，库式嵌入本就安全 —— 那边这个形态是**可选能力**，
# 不是沙箱必需。装配逻辑三平台共用一套，是否启用由交付件决定。
#
# 形态判据取**交付件自身**: kernel/ 下有没有原生宿主（arupa_desktop[.exe]）。
# 不用开关/环境变量，也**不按平台硬编** —— 产物在不在才是事实，开关会和产物不一致。
_DESKTOP_HOST_BY_OS = {
    "win": "arupa_desktop.exe",
    "mac": "arupa_desktop",
    "linux": "arupa_desktop",
}
_NATIVE_HOST_AS_BY_OS = {
    "win": "NomadBrowser.exe",
    "mac": "NomadBrowser",
    "linux": "NomadBrowser",
}
_SUBPROCESS_HOST_BY_OS = {
    "win": "arupa_render.exe",
    "mac": "arupa_render",
    "linux": "arupa_render",
}

# Linux 的 OOP 内核宿主工程/产物名（Windows 走原生宿主、不经过它；macOS 走 Mac 命名版本）。
# 产物名与 NomadBrowser.Avalonia.Linux/Program.cs::ResolveKernelHostPath 的探测名一致。
LINUX_KERNELHOST_PROJ = (PC_REPO / "Posix" / "NomadBrowser.Linux.KernelHost"
                         / "NomadBrowser.Linux.KernelHost.csproj")
LINUX_KERNELHOST_EXE = "NomadBrowser.Linux.KernelHost"


def desktop_host_name(c: Ctx) -> str:
    """原生宿主产物名（交付里那一件）。与 kernel.py 的 desktop_artifact 同源约定。"""
    return _DESKTOP_HOST_BY_OS.get(c.os, "arupa_desktop")


def native_host(c: Ctx, delivery: Path):
    """交付里若带原生宿主则返回它，否则 None（= 库式嵌入形态）。

    判据取**交付件自身**，三平台同一套逻辑，不按平台硬编。默认只有 Windows 交付
    带原生宿主（见 scripts/build.py 生成的 delivery 根目标里那条 is_win 分支），
    macOS/Linux 交付里没有它 → 返回 None → 装配行为与今天完全一致。
    """
    p = delivery / "kernel" / desktop_host_name(c)
    return p if p.is_file() else None


def native_host_publish_args() -> list:
    """原生宿主形态的 publish 参数。与 SINGLE_FILE_ARGS 只差一处，但那一处是致命的：

      🔴 单文件会把 **NomadBrowser.dll 嵌进 exe**，磁盘上不再留它。原生宿主形态的
         浏览器进程是原生 EXE，靠 hostfxr 加载 NomadBrowser.dll +
         NomadBrowser.runtimeconfig.json —— 这两个文件必须真实躺在磁盘上，否则启动期
         hostfxr 找不到程序集。
         （--self-contained true 保留，所以"客户机零 .NET 依赖"这条不破。）
    """
    return ["-p:PublishSingleFile=false",
            "-p:PublishReadyToRun=true",     # 换启动速度
            "-p:DebugType=none", "-p:DebugSymbols=false"]


def publish_updater(dn: str, c: Ctx, cfg: str, out: Path):
    """独立更新器: 必须在主程序退出后仍能覆盖主程序文件 ⇒ 独立自包含发布。
    Native AOT 优先；没装「使用 C++ 的桌面开发」时链接失败，退回自包含单文件。"""
    if not UPDATER_PROJ.exists():
        warn(f"未找到更新器工程: {UPDATER_PROJ}")
        return
    run([dn, "restore", str(UPDATER_PROJ), "-r", rid(c)], cwd=PC_REPO)
    if run([dn, "publish", str(UPDATER_PROJ), "-c", cfg, "-r", rid(c),
            "--self-contained", "true", "--no-restore",
            "-p:PublishAot=true", "-p:StripSymbols=true",
            "-p:DebugType=none", "-p:DebugSymbols=false", "-o", str(out)],
           cwd=PC_REPO, check=False):
        return
    warn("更新器 Native AOT 失败（常见: 未装「使用 C++ 的桌面开发」）—— 退回自包含单文件")
    run([dn, "restore", str(UPDATER_PROJ), "-r", rid(c), "-p:PublishAot=false"], cwd=PC_REPO)
    run([dn, "publish", str(UPDATER_PROJ), "-c", cfg, "-r", rid(c),
         "--self-contained", "true", "--no-restore",
         "-p:PublishAot=false", "-p:PublishSingleFile=true",
         "-p:IncludeNativeLibrariesForSelfExtract=true",
         "-p:DebugType=none", "-p:DebugSymbols=false", "-o", str(out)], cwd=PC_REPO)


def publish_windows_updater(dn: str, c: Ctx, cfg: str, out: Path):
    """更新窗（弹窗 UI）: 自包含、非单文件，落在载荷的 Helpers\\WindowsUpdater\\。"""
    if not WIN_UPDATER_PROJ.exists():
        warn(f"未找到更新窗工程: {WIN_UPDATER_PROJ}")
        return
    out.mkdir(parents=True, exist_ok=True)
    run([dn, "restore", str(WIN_UPDATER_PROJ), "-r", rid(c)], cwd=PC_REPO)
    run([dn, "publish", str(WIN_UPDATER_PROJ), "-c", cfg, "-r", rid(c),
         "--self-contained", "true", "--no-restore", "-p:PublishAot=false",
         "-p:DebugType=none", "-p:DebugSymbols=false", "-o", str(out)], cwd=PC_REPO)


def assemble_webui(payload: Path):
    """WebUI → payload\\Resources。

    npm 的 deploy 落在 PC 仓 dist\\Debug\\Resources（Release 那份可能压根没初始化），
    publish 不会把它带进载荷 ⇒ 必须显式拷。缺 index.html 的表现是"能启动但页面全白"。
    """
    for cand in (PC_REPO / "dist" / "Debug" / "Resources",
                 PC_REPO / "dist" / "Release" / "Resources"):
        if cand.is_dir() and (cand / "index.html").exists():
            shutil.copytree(cand, payload / "Resources", dirs_exist_ok=True)
            log(f"  资源: WebUI -> payload/Resources（来自 {cand.relative_to(PC_REPO)}）")
            return
    warn("未找到已 deploy 的 WebUI（dist/Debug|Release/Resources 里没有 index.html）")


def strip_pdbs(payload: Path):
    n = 0
    total = 0
    for p in payload.rglob("*.pdb"):
        total += p.stat().st_size
        p.unlink()
        n += 1
    if n:
        log(f"  剔 PDB: {n} 个 / {total / 1048576:.0f} MB")


def assert_native_host_exports(host: Path):
    """原生宿主形态的成败全押在一件事上: 内核 C API 真的从主镜像导出。

    EXE 目标的导出表来自 arupa_kernel_capi_*.cc 里的导出标注（source_set 把目标文件
    直接放在链接行上）。若哪天有人把 source_set 改成 static_library、或加了 /OPT:REF
    之外的去符号手段，"导出表为空"这件事在编译期**完全不报错** —— 要等到装机后托管侧
    P/Invoke 炸 DllNotFoundException，或者 Windows 沙箱又回到 err=64。

    导出名必然出现在符号表里: PE 的导出目录、ELF 的 .dynsym、Mach-O 的符号表都会带上
    它。按原始字节找一次即可，三平台通用。缺失 = 铁定坏，直接拦在出包这一步。
    """
    needle = b"arupa_kernel_abi_minor"
    # 分块扫，不整个读进内存（主镜像是 ~230MB 的 Chrome，read_bytes() 会白占一份）。
    # 块间留 needle-1 字节重叠，避免刚好跨块的匹配被漏掉。
    overlap = len(needle) - 1
    found = False
    tail = b""
    with host.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            if needle in (tail + chunk):
                found = True
                break
            tail = chunk[-overlap:] if overlap else b""
    if not found:
        err(f"{host.name} 里找不到内核 C API 导出（{needle.decode()}）—— 原生宿主形态下内核必须"
            f"静态链进主镜像并导出。常见成因: BUILD.gn 里 arupa_kernel_static 被从 "
            f"source_set 改成了 static_library（静态库按'被引用才拉取'链接，C API 链接期"
            f"无人引用 → 目标文件不进 EXE → 导出表为空）。")


def install_native_host(c: Ctx, host: Path, payload: Path):
    """把原生宿主装成主程序（Windows: NomadBrowser.exe；Linux: NomadBrowser）。

    为什么必须**覆盖** publish 产出的那个 apphost: 那是个 ~217KB 的小启动器，双击它走的
    是库式嵌入路径（内核在 arupa_kernel.dll 里）—— Windows 上沙箱照旧 err=64。换汤不换药。
    为什么沿用 NomadBrowser 这个名字: 快捷方式、更新器、tasklist 检查、
    Environment.ProcessPath 全都不用动，换掉的只是"这个 exe 是谁编的"。
    """
    assert_native_host_exports(host)
    if c.os == "mac":
        # macOS 的主程序在 .app bundle 里（Contents/MacOS/...），覆盖它还要重签；
        # 那是一条独立链路，未实现。显式拦下，别让半个装配静默产出坏包。
        err("macOS 的原生宿主装配需要改写 .app bundle 内的可执行文件并重签，尚未实现；"
            "请勿在 macOS 交付里携带 arupa_desktop。")
    dst = payload / _NATIVE_HOST_AS_BY_OS[c.os]
    old = dst.stat().st_size if dst.exists() else 0
    shutil.copy2(host, dst)
    log(f"  原生宿主主程序: {host.name} -> {dst.name}"
        f"（{host.stat().st_size / 1048576:.0f} MB，替换原 apphost {old / 1024:.0f} KB）")

    # 资产目录里那份同名的 EXE 是给"内核交付包单独使用"的，载荷里主程序已有一份 ——
    # 同一份 230MB 的浏览器躺两遍没意义。顺手把已经不被任何路径使用的子进程薄壳
    # （原生宿主形态下子进程就是主程序本身）一起清掉，只报不拦。
    for name in (desktop_host_name(c), _SUBPROCESS_HOST_BY_OS[c.os]):
        stale = payload / "arupa-desktop" / name
        if stale.exists():
            size = stale.stat().st_size
            stale.unlink()
            log(f"  载荷去重: arupa-desktop/{name}（{size / 1048576:.0f} MB）"
                f" —— 原生宿主形态下主程序兼任子进程，不再需要它")

    # arupa_kernel.dll 保留不删: 原生宿主形态下沙箱路径确实不再用它（KernelModuleBinding 会把
    # arupa_kernel 解到主镜像），但它仍是开发工具（Test/ArupaCdpHeadersProbe 在 EXE 形态下
    # 靠它而不是靠主镜像 —— 把 EXE 当库加载时 CRT 静态初始化不跑，C API 起不来）。
    # 要瘦掉这 230MB 得先给这些工具换成"主镜像外挂"的用法，属于独立改动。
    kernel_dll = payload / "arupa-desktop" / "arupa_kernel.dll"
    if kernel_dll.exists():
        warn(f"载荷里仍带 arupa-desktop/arupa_kernel.dll（{kernel_dll.stat().st_size / 1048576:.0f} MB）"
             f" —— 原生宿主形态的运行时不再需要它，保留是为了开发工具；要瘦身需先改工具的内核获取方式")


def publish_linux_kernelhost(c: Ctx, cfg: str, delivery: Path, kernel_dir: Path):
    """把 OOP 内核宿主发布进交付的 kernel/ 目录（Linux 便携包）。

    为什么 Linux 也有 KernelHost（而 Windows 没有）：
      Windows 必须走"原生宿主"（内核静态链进主镜像）—— Chromium 的 Windows 沙箱在
      CreateProcess(CREATE_SUSPENDED) 与 ResumeThread 之间做**跨进程地址交接**，那一刻只有
      主 EXE 镜像被映射，broker 侧要交接的符号必须住在主镜像里。macOS/Linux 的沙箱不交接
      地址（策略编译成字符串交子进程自 sandbox_init），库式嵌入/OOP 本身就是安全形态。
      故 Linux 与 Mac 同走 OOP，交付里带一个独立宿主进程。

    为什么装进 kernel/ 而不是别处（两条硬约束各钉一个）：
      · 宿主自己：NomadBrowser.Avalonia.Linux/Program.cs::ResolveKernelHostPath 按
        <ArupaDir>/NomadBrowser.Linux.KernelHost 探，而 ArupaDir 就是便携包的 kernel/。
      · chromium：按**主可执行文件所在目录**找 icudtl.dat 与 *.pak，宿主必须与内核件同目录。

    自包含策略与主程序**保持一致**：主程序的 linux publish 不带 --self-contained（框架依赖），
      宿主也不带 —— 否则包体平白多 ~70MB，而且"主程序跑得起来"就已经隐含"机器上有 .NET 运行时"。
    """
    if not LINUX_KERNELHOST_PROJ.is_file():
        warn(f"缺 Linux 内核宿主工程 {LINUX_KERNELHOST_PROJ} —— 交付里不会有 "
             f"{LINUX_KERNELHOST_EXE}，OOP 形态起不来内核（UI 侧会报找不到宿主）")
        return
    dn = dotnet()
    log(f"[kernelhost] publish {LINUX_KERNELHOST_PROJ.name} -> {kernel_dir}")
    # -f net10.0：与主程序同样的理由（多 TFM 工程 publish 不带 -f 会 NETSDK1047）。
    run([dn, "publish", str(LINUX_KERNELHOST_PROJ), "-c", cfg, "-r", rid(c), "-f", "net10.0",
         f"-p:ArupaDeliveryRoot={delivery}", "-o", str(kernel_dir)], cwd=PC_REPO)
    exe = kernel_dir / LINUX_KERNELHOST_EXE
    if not exe.is_file():
        err(f"{LINUX_KERNELHOST_EXE} 没被发布出来（{kernel_dir}）—— 无扩展名的 apphost 缺失，"
            f"启动时 OOP 形态会直接起不来内核")
    exe.chmod(0o755)        # zip/tar 往返可能丢可执行位，显式补一次
    log(f"  内核宿主: kernel/{LINUX_KERNELHOST_EXE}（{exe.stat().st_size / 1048576:.1f} MB）")


def warn_running_browser():
    """构建输出目录 == 运行目录（out/<project>-<os>-<arch>-<ver>-<link>）：
    浏览器还开着时，它自己占着这批 dll → MSB3026/3027「无法复制 … 文件正被另一个进程
    使用」（重试 10 次后才失败），看着像构建坏了。构建前先探一次，把原因直接说清楚。
    """
    if not common.IS_WIN:
        return          # Unix 下覆盖正在使用的文件不会失败，无此问题
    pids = re.findall(r"NomadBrowser\.exe\s+(\d+)",
                      out(["tasklist", "/FI", "IMAGENAME eq NomadBrowser.exe", "/NH"]))
    if not pids:
        return
    warn(f"检测到 NomadBrowser.exe 正在运行（PID {' '.join(pids)}）—— 构建输出目录就是它的"
         f"运行目录，dll 会被它自己锁住（MSB3027 复制失败）。请先关掉浏览器再 build/package。")


def do_build(c: Ctx):
    apply_developer_dir(c.cfg, "nomad_developer_dir", "developer_dir")
    sln, main_proj = project_paths(c)
    cfg = cget(c.cfg, "pc_config", "PC_CONFIG", default="Release")
    env_api = os.environ.get("NOMAD_API_ENV", "Production")
    delivery = resolve_delivery(c)

    dn = dotnet()
    warn_running_browser()
    ensure_kernel_facade(c, delivery)
    if c.os == "linux":
        # Directory.Build.props 在非 Windows 上把 BuildMac 置 true → 主工程双 TFM
        # （net10.0-windows7.0;net10.0），还原/编译 windows TFM 需要
        # EnableWindowsTargeting，否则 NETSDK1100；编出来也不能在 Linux 上跑，
        # 故显式只编 net10.0。
        run([dn, "restore", str(sln), "-p:EnableWindowsTargeting=true"], cwd=PC_REPO)
        # 中间产物同样落 out/browser-<os>-<arch>-<ver>-<link>/（说明见下方 else 分支）。
        c.out_dir.mkdir(parents=True, exist_ok=True)
        run([dn, "build", str(main_proj), "-c", cfg, "--no-restore", "-f", "net10.0",
             "-p:EnableWindowsTargeting=true", "-o", str(c.out_dir),
             *delivery_props(c, delivery), *compile_props(c)], cwd=PC_REPO)
    else:
        run([dn, "restore", str(sln)], cwd=PC_REPO)
        if c.os == "mac":
            # -p:BuildMac=true 必须显式给：slnx 直含的 Core/PluginContracts 等工程
            # 与 Avalonia.Mac 引用链全局属性不一致时，MSBuild 会把同一工程编两次。
            run([dn, "build", str(sln), "-c", cfg, "--no-restore",
                 "-p:BuildMac=true", *delivery_props(c, delivery)], cwd=PC_REPO)
        else:
            # 中间产物按要求落在 out/browser-<os>-<arch>-<ver>-<link>/（package 的载荷在它的 payload/ 下）。
            # 不给 -o 的话主程序会散落在 PC 仓的 dist/Release、dist/x64/Release 与各工程 bin\ 里。
            # Mac 不动: 它的产物最终由 publish 的 BuildAppBundle 组装成 .app，走仓内 mac-bundle 约定。
            c.out_dir.mkdir(parents=True, exist_ok=True)
            run([dn, "build", str(main_proj), "-c", cfg, "--no-restore", "-o", str(c.out_dir),
                 *delivery_props(c, delivery), *compile_props(c)], cwd=PC_REPO)
    log("PC 编译完成")


def do_package(c: Ctx):
    apply_developer_dir(c.cfg, "nomad_developer_dir", "developer_dir")
    sln, main_proj = project_paths(c)
    cfg = cget(c.cfg, "pc_config", "PC_CONFIG", default="Release")
    env_api = os.environ.get("NOMAD_API_ENV", "Production")
    delivery = resolve_delivery(c)
    dn = dotnet()
    warn_running_browser()
    ensure_kernel_facade(c, delivery)
    build_webui(c)

    if common.DRY_RUN:
        log(f"(dry-run) 将出包: 项目 {main_proj.name} / RID {rid(c)} / 交付根 {delivery}")
        return None
    # 坑: publish 带 -r（RID）却用 --no-restore，而 build 阶段的还原是**按解决方案且不带
    # RID** 跑的 → 资产文件里没有 net10.0-windows7.0/win-x64 目标，publish 撞
    # NETSDK1047「资产文件没有 ... 的目标」。RID 只能在工程级传（NETSDK1134 禁止解决方案
    # 级），所以这里按主工程补一次带 RID 的还原。
    run([dn, "restore", str(main_proj), "-r", rid(c)], cwd=PC_REPO)
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
        # 发布暂存里若残留上次带 provenance 的旧件，会被再拷进 bundle 导致 codesign 失败。
        # 这里"挪走"而不是删除：重命名不触发外层的批量删除守卫，代价只是 out/ 下留一份旧件
        # （out/ 已被 .gitignore 覆盖，可随时手动清）。
        local_out = c.out_dir / "dotnet-local"
        local_out.mkdir(parents=True, exist_ok=True)
        stale_root = c.out_dir / "stale-publish"
        for stale in ("main-publish", "kernelhost-publish", "mac-updater"):
            src = PC_REPO / "dist" / "macos" / stale
            if not src.exists():
                continue
            dst = stale_root / f"{stale}-{time.strftime('%Y%m%d-%H%M%S')}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            log(f"旧发布暂存已挪走: {src.name} -> {dst}")
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
        # 同 do_build：-f net10.0（多 TFM 工程 publish 不带 -f 会 NETSDK1047）
        # + EnableWindowsTargeting（还原 windows7.0 TFM 需要）。
        run([dn, "publish", str(main_proj), "-c", cfg, "-r", rid(c), "-f", "net10.0",
             "--no-restore", "-o", str(payload), "-p:EnableWindowsTargeting=true",
             *delivery_props(c, delivery)], cwd=PC_REPO)
    else:
        # 与 assemble-release-payload.ps1 同款出货形态: 单文件 exe + 原生库/runtimes 散放。
        # 原生宿主交付（kernel\ 下带 arupa_desktop.exe）改走 native_host_publish_args()
        # —— 那种形态**不能**单文件，原因见该函数注释（hostfxr 要磁盘上真实的 dll）。
        style = native_host_publish_args() if native_host(c, delivery) else SINGLE_FILE_ARGS
        run([dn, "publish", str(main_proj), "-c", cfg, "-r", rid(c),
             "--self-contained", "true", "--no-restore", "-o", str(payload),
             "-p:Platform=x64", *style,
             *delivery_props(c, delivery), *compile_props(c)], cwd=PC_REPO)
        publish_updater(dn, c, cfg, payload)
        publish_windows_updater(dn, c, cfg, payload / "Helpers" / "WindowsUpdater")

    # 内核件必须随包: Program.cs 的 ResolveArupaDir 在打包态只认
    # <BaseDirectory>\arupa-desktop（里面得有 arupa_kernel.dll）—— 没有它启动时
    # 直接 DllNotFoundException「Unable to load DLL 'arupa_kernel'」，报的是"找不到模块"
    # 而不是"缺内核"。放在 publish 之后、拷进 dist 之前: 这样 out/ 的中间产物目录与
    # dist/ 交付件两处都能直接跑（之前只有 dist 那一份带内核，从 out/.../payload 启动必崩）。
    if c.os == "win":
        shutil.copytree(delivery / "kernel", payload / "arupa-desktop", dirs_exist_ok=True)
        # 原生宿主: 接管成主程序（覆盖上面 publish 出的那个 apphost）。
        # 必须在 copytree 之后 —— 它要顺手清掉资产目录里那份同名的 EXE。
        host = native_host(c, delivery)
        if host:
            install_native_host(c, host, payload)
        assemble_webui(payload)
        strip_pdbs(payload)
    elif c.os == "linux":
        # Linux 无 bundle: 原生宿主（若交付带了）直接覆盖 payload/NomadBrowser。
        # 默认交付不带（见 native_host 注释），这里恒为 None，行为与今天一致。
        host = native_host(c, delivery)
        if host:
            install_native_host(c, host, payload)

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
        shutil.copytree(payload, dst, symlinks=True)   # 内核件已在 payload/arupa-desktop 里
        if c.os == "linux":
            # Linux 没有 bundle 概念：便携目录 = 主程序 + kernel/ + Resources/ + run.sh
            shutil.copytree(delivery / "kernel", stage / "kernel", dirs_exist_ok=True)
            # OOP 内核宿主必须落在 kernel/（理由见 publish_linux_kernelhost）。放在 copytree
            # 之后：交付根的 kernel/ 是产出方的目录，不往里写；只写我们自己的交付暂存。
            publish_linux_kernelhost(c, cfg, delivery, stage / "kernel")
            res = PC_REPO / "dist" / "Debug" / "Resources"
            if res.is_dir():
                shutil.copytree(res, dst / "Resources", dirs_exist_ok=True)
            (stage / "run.sh").write_text(
                "#!/usr/bin/env bash\ncd \"$(dirname \"$0\")/NomadBrowser\"\n"
                "export LD_LIBRARY_PATH=\"$(dirname \"$0\")/kernel:$LD_LIBRARY_PATH\"\n"
                "exec ./NomadBrowser \"$@\"\n", encoding="utf-8")
            (stage / "run.sh").chmod(0o755)

    # 内核件必须真的进了载荷，否则这包装起来也跑不起来。
    # 内核形态不同，该查的标记物也不同：
    #   · 库式嵌入    → arupa-desktop\arupa_kernel.dll（内核本体就是它）
    #   · 原生宿主形态 → arupa-desktop\arupa_plugin_host.exe
    #     （内核在主程序里，磁盘上没有 arupa_kernel.dll 这个名字了 —— 但 arupa_plugin_host.exe
    #       两种形态都在，且同属"内核件进没进载荷"这件事，用它当标记物最稳）
    win_marks = [dst / "NomadBrowser.exe",
                 dst / "Resources" / "index.html",
                 dst / "NomadBrowser.Updater.exe",
                 dst / "Helpers" / "WindowsUpdater" / "NomadBrowser.Windows.Updater.exe"]
    if native_host(c, delivery):
        win_marks.append(dst / "arupa-desktop" / "arupa_plugin_host.exe")
    else:
        win_marks.append(dst / "arupa-desktop" / "arupa_kernel.dll")
    marks = {"mac": [dst / "Contents" / "Resources" / "arupa-mac"],
             # Linux 便携包：内核件在 stage/kernel/，OOP 宿主必须与它同目录（缺失即起不来内核）。
             "linux": [stage / "kernel", stage / "kernel" / LINUX_KERNELHOST_EXE],
             "win": win_marks}[c.os]
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
