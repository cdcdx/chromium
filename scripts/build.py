#!/usr/bin/env python3
"""Build and package Arupa kernels and Nomad browsers without fetching sources."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

import deliveries
import fetch as F
import native_tools
import publish
from linux_shared_library import prepare_v8_tls
from concurrency import automatic_jobs
from apple_tools import prepare_mac_toolchain
from platforms import MATRIX, normalize_os, architectures, host_for

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ALIASES = {"desktop": "arupa_desktop", "android": "arupa_android"}



def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project", choices=("arupa_desktop", "arupa_android", "nomad_desktop", "nomad_android", "desktop", "android"))
    p.add_argument("actions", nargs="*", help="gen / build / package / publish / all（默认 all；浏览器仅 build + package；publish 仅内核）")
    p.add_argument("--os", type=normalize_os, choices=tuple(MATRIX), default="")
    p.add_argument("--args", type=Path, help="build 目录内的 GN 配置；默认 build/<os>/args.gn")
    p.add_argument("--arch", choices=("x86", "x64", "arm64", "all"), default="", help="默认：桌面为当前芯片，arupa_android 为 all，Android 浏览器为 arm64")
    p.add_argument("--delivery", type=Path, help="PC 浏览器使用的桌面内核交付包")
    p.add_argument("--pc-project", type=Path, help="PC 浏览器主 csproj；默认按系统定位")
    p.add_argument("--variant", choices=("debug", "release"), default="release", help="浏览器构建配置")
    p.add_argument("--no-web", action="store_true", help="PC 浏览器复用已有 WebUI 资源")
    p.add_argument("--dotnet", help="PC 浏览器的 dotnet 可执行文件；默认 dotnet_path 配置或 PATH")
    p.add_argument("--nuget-config", type=Path, help="PC 构建使用的 NuGet 配置；默认 build/nuget.config（官方 v3 源）")
    p.add_argument("--android-sdk", type=Path, help="Android 浏览器 SDK 目录；也可设置 ANDROID_HOME")
    p.add_argument("--ver", default="", help="内核版本；默认读取 src/chrome/VERSION，编译内核时必须与源码一致")
    p.add_argument("--link", "--mode", choices=("static", "dynamic"), default="static")
    p.add_argument("--jobs", "-j", type=int, default=None, help="并发任务数；Ninja 默认按 CPU/内存自动计算；显式指定可覆盖")
    p.add_argument("--zip", action="store_true", help="打包时额外生成 zip")
    p.add_argument("--num", type=int, default=None, help="交付序号（默认自动递增）")
    p.add_argument("--keep", type=int, default=2, help="package保留的交付份数，默认 2（含本次），0 = 保留全部、只清残留")
    p.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    p.add_argument("--dry-run", action="store_true", help="仅打印计划；允许在任意宿主预览平台矩阵")
    p.add_argument("--url", default="", help="publish 发布站点源，如 http://192.168.77.104:8080；默认取 .env 的 publish_url")
    p.add_argument("--token", default="", help="publish 上传令牌；默认取环境变量/.env 的 publish_upload_token")
    p.add_argument("--allow-http", action="store_true", help="publish 对公网/域名也允许 http://（内网私有地址已默认放行）")
    p.add_argument("--platform", default="", help="publish 元数据 platform；默认按交付包文件名推断（mac / win / linux / android）")
    p.add_argument("--file", default="", help="publish 指定交付包：路径，或直接给交付序号 n；默认取最大的 zip")
    return p


V8_SNAPSHOT_TARGET = "tools/v8_context_snapshot:generate_v8_context_snapshot"


def target_in_graph(out, target):
    """按 build.ninja 判断 ninja 目标是否在构建图里（GN 把标签里的 ':' 转义成 '$:'）。"""
    graph = out / "build.ninja"
    if not graph.is_file():
        return False
    return target.replace(":", "$:") in graph.read_text(encoding="utf-8", errors="replace")


def build_targets(project, target_os, arch, out):
    base = f"//chrome/browser/{project}"
    if project == "arupa_android":
        targets = [base + "/aar:arupa_kernel_aar", "//content/shell:pak"]
        # arm64 交付件必须带 v8_context_snapshot_64.bin：内核 .so 编译期带
        # USE_V8_CONTEXT_SNAPSHOT=true，运行期 gin 按名字在 assets 里找这个文件，缺了直接
        # FATAL（2026-09-28 事故）。该 action 只有被点名才会产出，所以显式加进 ninja 目标；
        # x64 上 use_v8_context_snapshot=false，目标不在图里，也不能传。
        if arch == "arm64":
            if target_in_graph(out, V8_SNAPSHOT_TARGET):
                targets.append(V8_SNAPSHOT_TARGET)
            else:
                F.log(f"注意：构建图里没有 {V8_SNAPSHOT_TARGET}（use_v8_context_snapshot 未生效），"
                      "交付件会缺少 v8_context_snapshot_64.bin")
        return targets
    targets = [base + ":arupa_kernel", "//content/shell:pak",
               "//third_party/hyphenation-patterns:bundle_hyphen_data"]
    if target_os in ("win", "mac", "linux"):
        targets += [base + ":render", base + "/plugin/host:arupa_plugin_host"]
    return targets


def verify_deps(version):
    """gn gen 前提示未同步的 DEPS 子仓库。

    未同步的独立仓库会让 GN 报 `Unable to load "//third_party/skia/gn/shared_sources.gni"`，
    看到的是"少了个文件"，看不到"DEPS 没同步完"这个真实原因；`fetch deps` 会按同一份清单
    （.gclient_entries）自动补拉这些空仓库。这里只警告、不拦截：有些依赖（ANGLE/Dawn 的测试
    数据）跟本次目标无关，不该因为它们挡住编译；真缺东西时 gn gen 会失败，由 gn_failure_hint
    精确指出是哪个依赖。"""
    broken = F.unsynced_deps()
    if broken is None:
        F.err(f"找不到 {F.WORKSPACE_ROOT / '.gclient_entries'}：DEPS 从未同步；"
              f"请先运行: bash fetch.sh deps --ver {version}")
    if not broken:
        return
    listing = "\n".join(f"  {relative}（应为 {url}）" for relative, url in broken[:10])
    if len(broken) > 10:
        listing += f"\n  …另有 {len(broken) - 10} 个"
    F.log(f"警告: {len(broken)} 个 DEPS 仓库没有同步完成（独立 git 仓库为空）:\n" + listing
          + f"\n若 gn gen 因缺文件失败，请先运行: bash fetch.sh deps --ver {version}")


def repository_path(path):
    """把 GN 报的路径归一成 src 下的相对路径。

    GN 报 `Unable to load "..."` 时给的是绝对路径（/…/src/third_party/skia/gn/x.gni），
    而 import 语句里是 `//third_party/skia/gn/x.gni`，两种都可能出现在输出里。"""
    text = path.replace("\\", "/")
    if text.startswith("//"):
        return text[2:]
    source = SRC.as_posix().rstrip("/")
    if text.startswith(source + "/"):
        return text[len(source) + 1:]
    return text.lstrip("./")


def gn_failure_hint(output, version):
    """把 GN 的 `Unable to load "…"` 翻译成「哪个 DEPS 仓库没同步」+ 修复命令。

    认不出来（GN 本身的配置错误等）就返回 None，让调用方原样抛出错误，不要掩盖真实信息。"""
    if not output:
        return None
    missing = sorted({repository_path(path) for path in re.findall(r'Unable to load "([^"]+)"', output)})
    if not missing:
        return None
    owners = {}
    for relative, url, revision in (F.deps_entries() or []):
        prefix = relative.removeprefix("src/").rstrip("/")
        if prefix:
            owners[prefix] = (relative, f"{url}@{revision}" if revision else url)
    lines, matched = [], False
    for path in missing:
        best = max((prefix for prefix in owners
                    if path == prefix or path.startswith(prefix + "/")), key=len, default=None)
        matched = matched or best is not None
        lines.append(f"  //{path}" + (f"  ← {owners[best][0]}（应为 {owners[best][1]}）" if best else ""))
    head = "GN 生成失败：缺少以下 DEPS 依赖（独立 git 仓库为空）:" if matched else "GN 生成失败：缺少以下文件:"
    return (head + "\n" + "\n".join(lines[:10])
            + f"\n请运行: bash fetch.sh deps --ver {version}（按 .gclient_entries 自动补拉空仓库）")


def args_template(target_os, explicit=None):
    path = (explicit.expanduser() if explicit else ROOT / "build" / target_os / "args.gn").resolve()
    directory = (ROOT / "build").resolve()
    if directory not in path.parents or not path.is_file():
        F.err(f"--args 必须指定 build 目录内已有的 GN 配置文件: {path}")
    return path


def render_args(path, target_os, arch, link):
    if target_os == "android" and link != "static":
        F.err("Android 仅支持 static，不能生成动态构建配置")
    text = path.read_text(encoding="utf-8-sig")
    configured_os = re.findall(r'^[ \t]*target_os[ \t]*=[ \t]*"([^"\n]+)"', text, re.M)
    if configured_os != [target_os]:
        F.err(f"{path} 必须包含唯一的 target_os = \"{target_os}\"，与目标系统一致")
    settings = {"target_cpu": json.dumps(arch),
                "is_component_build": "true" if link == "dynamic" else "false",
                "use_siso": "false", "use_remoteexec": "false"}
    if target_os == "android":
        settings["include_both_v8_snapshots"] = "true" if arch == "arm64" else "false"
        # 与交付口径一致：只有 arm64 用 v8 上下文快照；其余 ABI 必须显式 false，
        # 否则 args.gn 里残留的 true 会让 x64 也编出 v8_context_snapshot_64.bin 被打进交付。
        # 注意：**不要**动 use_v8_context_snapshot_android_secondary_abi —— 64 位图上
        # android_app_secondary_abi 有定义，gin 的 v8_snapshot_secondary_abi_assets 会因此
        # 去要 32 位那份 v8_context_snapshot_32.bin，而本图的 root-target 闭包里没有二级
        # ABI 工具链的生成动作，gn gen 会直接报 "Input to target not generated by a
        # dependency"；交付件本来也只要 arm64 的 _64 那份。
        settings["use_v8_context_snapshot"] = "true" if arch == "arm64" else "false"
    if target_os == "linux":
        settings["blink_heap_inside_shared_library"] = "true"
        settings["v8_tls_used_in_library"] = "true"
    for key, value in settings.items():
        # GN evaluates args sequentially: keep assignments before later uses.
        pattern = re.compile(rf"(?m)^([ \t]*){key}[ \t]*=[^\n]*")
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            F.err(f"{path}: {key} 有多处赋值，请改为一处明确的配置")
        if matches:
            text = pattern.sub(lambda match: f"{match[1]}{key} = {value}", text)
        else:
            text = f"{key} = {value}\n" + text
    return text


def write_if_changed(path, text):
    F.log(f"生成 {path}")
    if not F.DRY_RUN:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            # Replace atomically so interruption never leaves half an args file.
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temporary = Path(temporary)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


def exclude_generated(paths):
    """把模块挂载和构建入口写进 src 的本地 Git exclude，避免被当成未跟踪改动。"""
    F.exclude_paths(SRC, paths, "模块挂载和构建入口")


def prepare_project(project):
    source = ROOT / project
    mount = SRC / "chrome/browser" / project
    if not (source / "BUILD.gn").is_file():
        F.err(f"缺少项目源码: {source}/BUILD.gn")
    if os.path.lexists(mount):
        if mount.resolve() != source.resolve():
            F.err(f"{mount} 已存在且不是指向 {source} 的链接；请先核对现有源码")
    else:
        F.log(f"挂载 {mount} -> {source}")
        if not F.DRY_RUN:
            mount.parent.mkdir(parents=True, exist_ok=True)
            if F.IS_WIN:
                F.run(["cmd", "/c", "mklink", "/J", mount, source])
            else:
                mount.symlink_to(source, target_is_directory=True)
    exclude_generated([f"chrome/browser/{project}", "arupa_build/"])
    # A separate GN root keeps upstream BUILD.gn clean when switching versions.
    # --root-target already limits the graph to this group's dependency closure.
    graph = SRC / "arupa_build" / project / "BUILD.gn"
    lines = ['# Generated by scripts/build.py; do not edit.', 'group("delivery") {', '  testonly = true']
    if project == "arupa_desktop":
        # bundle_hyphen_data: 产出 out/hyphen-data/{manifest.json,*.hyb}，正是交付包 kernel/hyphen-data/
        # 那一份（PC 侧 Mac/ArupaDelivery.props 逐件校验它）。它默认不在本工程的图里，得显式挂进来。
        lines += ['  deps = [ "//chrome/browser/arupa_desktop:arupa_kernel", "//content/shell:pak",',
                  '            "//third_party/hyphenation-patterns:bundle_hyphen_data" ]',
                  # 原生宿主形态: 内核静态链进 arupa_desktop, 子进程由同一 EXE 经
                  # --type= 分流拉起 —— Windows 沙箱要求 broker 符号住在主镜像, 只有
                  # 这一形态满足。
                  # ⚠ 必须显式挂进来: gn 只展开从 delivery 出发可达的依赖闭包, 目标没被
                  #   引用就会被**静默剪掉**, 表现为 out/ 里根本没有 arupa_desktop —— 而
                  #   构建仍然"成功"、打包脚本也只是安静地按旧形态出包。这就是原生宿主
                  #   落地后"从未编出"的根因 (BUILD.gn 里目标一直在, 图里一直没它)。
                  # 三平台都能编这个目标; 这里只在 Windows 带 —— macOS/Linux 的沙箱不做
                  #   跨进程地址交接, 库式嵌入本就安全, 该形态是可选能力。要在某平台启用:
                  #   把下面的 is_win 放宽成 is_win || is_mac || is_linux, 装配层与打包
                  #   脚本都是形态感知的, 会自动跟着切, 不需要改别处。
                  '  if (is_win) { deps += [ "//chrome/browser/arupa_desktop:arupa_desktop" ] }',
                  '  if (is_win || is_mac || is_linux) { deps += [ "//chrome/browser/arupa_desktop:render" ] }',
                  '  if (is_win || is_mac || is_linux) { deps += [ "//chrome/browser/arupa_desktop/plugin/host:arupa_plugin_host" ] }']
    else:
        lines += ['  deps = [ "//chrome/browser/arupa_android/aar:arupa_kernel_aar", "//content/shell:pak" ]']
    write_if_changed(graph, "\n".join(lines + ['}', '']))


def kernel_delivery_prefix(target_os, cpu, version):
    """内核交付目录前缀，与 scripts/packaging/package-arupa_*.sh 的命名保持一致。

    桌面：`arupa-<os>-<arch>-<ver>-static-<n>`；Android：`arupa-android-<ver>-static-<n>`
    （一个交付里含两个 ABI，序号在所有架构间共用）。package 后按此前缀裁剪旧交付。"""
    if target_os == "android":
        return f"arupa-android-{version}-static-"
    return f"arupa-{target_os}-{cpu}-{version}-static-"


def package_command(args, target_os, arch, version):
    if target_os == "win":
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   ROOT / "scripts/packaging/package-arupa_desktop.ps1"]
    else:
        name = "android" if target_os == "android" else "desktop"
        command = ["bash", ROOT / f"scripts/packaging/package-arupa_{name}.sh"]
    command += ["--os", target_os, "--arch", arch, "--ver", version,
                "--dist-dir", args.dist_dir.resolve()]
    if args.zip:
        command.append("--zip")
    if args.num is not None:
        command += ["--num", str(args.num)]
    return command


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    F.DRY_RUN = args.dry_run
    project = ALIASES.get(args.project, args.project)
    # 浏览器工程与内核工程按前缀区分：arupa_* 是内核，nomad_* 是浏览器外壳。
    is_browser = project in ("nomad_desktop", "nomad_android")
    is_android = project in ("arupa_android", "nomad_android")
    target_os = args.os or ("android" if is_android else F.HOST_OS)
    target_os = normalize_os(target_os)
    if target_os not in MATRIX or is_android != (target_os == "android"):
        p.error("桌面项目仅支持 win/mac/linux；Android 项目仅支持 android")
    machine = (os.environ.get("PROCESSOR_ARCHITEW6432") if F.HOST_OS == "win" else None) or platform.machine()
    host_cpu = {"aarch64": "arm64", "amd64": "x64", "x86_64": "x64", "i386": "x86", "i686": "x86"}.get(machine.lower(), machine.lower())
    arch = args.arch or ("all" if project == "arupa_android" else "arm64" if is_android else host_cpu)
    try:
        arches = architectures(target_os, arch)
    except ValueError as exc:
        p.error(str(exc))
    actions = args.actions or ["all"]
    if set(actions) - {"gen", "build", "package", "publish", "all"}:
        p.error("动作只支持 gen / build / package / publish / all；依赖下载请使用 fetch")
    actions = set(actions)
    if "all" in actions:
        # publish 不并入 all：发版是显式动作，不该因为一条 all 把产物推出去。
        actions = {"build", "package"} if is_browser else {"gen", "build", "package"}
    if "publish" in actions and is_browser:
        p.error("publish 仅支持内核交付（arupa_desktop / arupa_android）")
    if args.link == "dynamic" and (is_browser or target_os == "android" or "package" in actions):
        p.error("Android、浏览器和交付打包仅支持 static；arupa_desktop dynamic 可执行 gen/build")
    if (args.jobs is not None and args.jobs < 1) or (args.num is not None and args.num < 1):
        p.error("--jobs / --num 必须大于 0")
    if args.keep < 0:
        p.error("--keep 不能为负数")
    if args.jobs is None:
        if "build" in actions and not is_browser:
            args.jobs, reason = automatic_jobs()
            F.log(f"自动并发任务数: {args.jobs}（{reason}）")
        else:
            args.jobs = 8 if is_browser else 1
    elif "build" in actions:
        F.log(f"使用指定并发任务数: {args.jobs}")
    required_host = host_for(target_os)
    # 只推送不构建时放宽宿主限制：交付包可能是在别的机器上打好的。
    if not args.dry_run and F.HOST_OS != required_host and actions != {"publish"}:
        p.error(f"{target_os} 构建需要 {required_host} 宿主（当前 {F.HOST_OS}）")
    version = args.ver if is_browser and args.ver else F.chromium_version()
    if args.ver and args.ver != version:
        p.error(f"--ver {args.ver} 与 src/chrome/VERSION {version} 不一致，请先 fetch 指定版本")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        p.error("版本必须是 Chromium 四段版本号 X.Y.Z.W")
    if is_browser:
        import nomad
        nomad.run(ROOT, args, project, target_os, arches, version, actions)
        F.log("浏览器构建计划完成（未执行）" if args.dry_run else "浏览器构建流程完成")
        return 0
    if "package" in actions and not args.dry_run:
        attachments = ROOT / "package" / ("package_android" if is_android else "package_desktop")
        if not attachments.is_dir():
            F.err(f"缺少交付附件目录: {attachments}")
    cfg = F.load_config()
    F.tool_environment(cfg)  # PATH only; never bootstrap or fetch during build.
    if target_os == "mac" and actions & {"gen", "build"}:
        prepare_mac_toolchain(cfg)
    if actions & {"gen", "build"}:
        if not (SRC / "BUILD.gn").is_file():
            F.err("缺少 Chromium 源码，请先 fetch")
        gn = SRC / {"win": "buildtools/win/gn.exe", "mac": "buildtools/mac/gn", "linux": "buildtools/linux64/gn"}[required_host]
        ninja = SRC / ("third_party/ninja/ninja.exe" if target_os == "win" else "third_party/ninja/ninja")
        if not args.dry_run:
            for tool in (gn, ninja):
                if not tool.is_file():
                    F.err(f"缺少工具 {tool}；请先运行 fetch（含 hooks）")
            verify_deps(version)
        config_path = args_template(target_os, args.args)
        templates = {cpu: render_args(config_path, target_os, cpu, args.link) for cpu in arches}
        native_tools.kernel_check(target_os, arches)
        if target_os == "linux":
            if args.dry_run:
                F.log("(dry-run) 检查 V8 共享库 TLS 配置")
            else:
                try:
                    prepare_v8_tls(SRC)
                except (OSError, ValueError) as exc:
                    F.err(str(exc))
        F.log(f"GN 配置: {config_path}（arch={arch}, link={args.link}）")
        prepare_project(project)
        for cpu in arches:
            out = SRC / "out" / f"arupa-{target_os}-{cpu}-{version}{'-static' if args.link == 'static' else ''}"
            template = templates[cpu]
            stamp = out / "arupa-build.json"
            identity = {"project": project, "os": target_os, "arch": cpu, "version": version, "link": args.link}
            if "gen" in actions or ("build" in actions and not (out / "build.ninja").exists()):
                # Invalidate the old success marker BEFORE changing configuration.
                # A failed gen must never authorize building the previous graph.
                if not args.dry_run:
                    stamp.unlink(missing_ok=True)
                write_if_changed(out / "args.gn", template)
                root_target = f"//arupa_build/{project}:delivery"
                # 同一标签再传 --root-pattern 生成的 build.ninja 与只传 --root-target 完全一致
                # （实测 24784 条 build 规则逐字节相同），却会让 GN 额外做一次生成输入校验，
                # 在 Android 的 java build_config 目标上误报 58 个 "Input to target not
                # generated by a dependency" 并以退出码 1 结束，所以只限制 root target。
                try:
                    F.run([gn, "gen", out, f"--root-target={root_target}"], SRC, tee=True)
                except subprocess.CalledProcessError as error:
                    # GN 把错误打在 stdout、进度打在 stderr，两条流都要看。
                    hint = gn_failure_hint(f"{error.stderr or ''}\n{error.output or ''}", version)
                    if hint is None:
                        raise
                    F.err(hint)
                write_if_changed(stamp, json.dumps(identity, indent=2) + "\n")
            elif "build" in actions and not args.dry_run:
                # args.gn 由本脚本以 UTF-8 写出（含中文注释），必须显式按 UTF-8 回读：
                # Windows 中文环境的默认编码是 GBK，回读时会直接抛 UnicodeDecodeError。
                if not stamp.exists() or json.loads(stamp.read_text(encoding="utf-8")) != identity \
                        or (out / "args.gn").read_text(encoding="utf-8") != template:
                    F.err(f"{out} 配置不匹配；请先执行 gen")
            if "build" in actions:
                F.run([ninja, "-C", out, "-j", str(args.jobs),
                       *[t.removeprefix('//') for t in build_targets(project, target_os, cpu, out)]], SRC)
    if "package" in actions:
        # Android's packager combines both ABIs in one delivery when arch=all.
        package_arches = (arch,) if target_os == "android" else arches
        dist_dir = args.dist_dir.resolve()
        for cpu in package_arches:
            prefix = kernel_delivery_prefix(target_os, cpu, version)
            if args.dry_run:
                F.log(f'(dry-run) 清理残留（*.trash-*/.publish-* 等）并保留最近 {args.keep} 份交付: '
                      f'{dist_dir}/{prefix}<n>')
            else:
                # 打包前清掉中断/反复重建留下的整份旁置拷贝（每个近 800MB），顺带腾出磁盘。
                deliveries.clean_stale_directories(dist_dir)
            F.run(package_command(args, target_os, cpu, version), ROOT)
            if not args.dry_run:
                # 只在本次打包成功后裁剪旧交付：失败时上一份仍留着可回退。
                deliveries.prune(dist_dir, prefix, args.keep,
                                 args.num or deliveries.newest_number(dist_dir, prefix))
    if "publish" in actions:
        dist_dir = args.dist_dir.resolve()
        prefix = publish.delivery_prefix(target_os, arch, version, args.link)
        requested = publish.selector(args.file, args.num)
        if requested is None and "package" in actions and not args.dry_run:
            # 同一次调用里刚打过包：优先推刚产出的那一份，而不是「碰巧最大的 zip」。
            requested = deliveries.newest_number(dist_dir, prefix) or None
        archive = publish.select_archive(dist_dir, prefix, requested)
        identity = publish.parse_artifact(archive.name)
        if args.ver and args.ver != identity.version:
            F.err(f"--ver {args.ver} 与交付包 {archive.name} 的版本不一致")
        publish.push(archive,
                     url=args.url or F.cget(cfg, "publish_url"),
                     token=args.token or F.cget(cfg, "publish_upload_token"),
                     allow_http=args.allow_http,
                     platform=args.platform,
                     version=identity.version)
    F.log("构建流程完成" if not args.dry_run else "构建计划完成（未执行）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
