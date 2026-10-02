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
import shlex
import subprocess
import sys
import tempfile

import fetch as F
import native_tools

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
MATRIX = {"win": ("x86", "x64", "arm64"), "mac": ("x64", "arm64"),
          "linux": ("x86", "x64", "arm64"), "android": ("x64", "arm64")}
ALIASES = {"desktop": "arupa_desktop", "android": "arupa_android"}


def prepare_mac_toolchain(cfg):
    """Select full Xcode for this process and fail before modifying build files."""
    configured = F.cget(cfg, "chromium_developer_dir") or os.environ.get("DEVELOPER_DIR", "")
    if F.DRY_RUN:
        F.log(f"(dry-run) 检查完整 Xcode: {configured or '系统选择或已安装 Xcode'}")
        return
    if configured:
        developer = Path(configured).expanduser()
    else:
        selected = subprocess.run(['xcode-select', '-p'], capture_output=True, text=True)
        developer = Path(selected.stdout.strip()) if selected.returncode == 0 else Path('/nonexistent')
        if not (developer / 'usr/bin/xcodebuild').is_file():
            candidates = sorted({p / 'Contents/Developer'
                                 for base in (Path('/Applications'), Path.home() / 'Applications')
                                 for p in base.glob('Xcode*.app')
                                 if (p / 'Contents/Developer/usr/bin/xcodebuild').is_file()})
            if len(candidates) > 1:
                F.err('发现多个 Xcode，请在 .env 中设置 chromium_developer_dir: ' + ', '.join(map(str, candidates)))
            if candidates:
                developer = candidates[0]
    if developer.suffix == '.app':
        developer = developer / 'Contents/Developer'
    if not (developer / 'usr/bin/xcodebuild').is_file():
        F.err(f'未找到完整 Xcode（当前路径: {developer}）。Command Line Tools 不能用于此构建。'
              '请安装完整 Xcode，并在 .env 设置 chromium_developer_dir=/Applications/Xcode.app/Contents/Developer'
              '（按实际安装位置修改）。')
    environment = dict(os.environ, DEVELOPER_DIR=str(developer))
    for command in (['/usr/bin/xcodebuild', '-version'], ['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path']):
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        if result.returncode:
            F.err(f'Xcode 预检查失败: {" ".join(command)}\n{result.stderr.strip() or result.stdout.strip()}')
    # xcrun --find can resolve a shim even when the downloadable toolchain is absent.
    metal = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', 'metal', '--version'],
                           capture_output=True, text=True, env=environment)
    if metal.returncode:
        install = shlex.join(['env', f'DEVELOPER_DIR={developer}', '/usr/bin/xcodebuild',
                              '-downloadComponent', 'MetalToolchain'])
        F.err(f'Metal 编译器不可用，请先安装当前 Xcode 的 Metal Toolchain：\n{install}\n'
              f'{metal.stderr.strip() or metal.stdout.strip()}')
    os.environ['DEVELOPER_DIR'] = str(developer)
    F.log(f'Xcode: {developer}')


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project", choices=("arupa_desktop", "arupa_android", "nomadbrowser.pc", "nomadbrowser.android", "desktop", "android"))
    p.add_argument("actions", nargs="*", help="gen / build / package / all（默认 all；浏览器仅 build + package）")
    p.add_argument("--os", choices=(*MATRIX, "windows", "macos"), default="")
    p.add_argument("--args", type=Path, help="build 目录内的 GN 配置；默认 build/<os>/args.gn")
    p.add_argument("--arch", choices=("x86", "x64", "arm64", "all"), default="", help="默认：桌面为当前芯片，arupa_android 为 all，Android 浏览器为 arm64")
    p.add_argument("--delivery", type=Path, help="PC 浏览器使用的桌面内核交付包")
    p.add_argument("--pc-project", type=Path, help="PC 浏览器主 csproj；默认按系统定位")
    p.add_argument("--variant", choices=("debug", "release"), default="release", help="浏览器构建配置")
    p.add_argument("--no-web", action="store_true", help="PC 浏览器复用已有 WebUI 资源")
    p.add_argument("--dotnet", help="PC 浏览器的 dotnet 可执行文件；默认 dotnet_path 配置或 PATH")
    p.add_argument("--android-sdk", type=Path, help="Android 浏览器 SDK 目录；也可设置 ANDROID_HOME")
    p.add_argument("--ver", default="", help="内核版本；默认读取 src/chrome/VERSION，编译内核时必须与源码一致")
    p.add_argument("--link", "--mode", choices=("static", "dynamic"), default="static")
    p.add_argument("--jobs", "-j", type=int, default=8)
    p.add_argument("--plugin-runtime", type=Path, help="nomad-plugin-runtime.js 路径；默认工作区 plugin-runtime/")
    p.add_argument("--zip", action="store_true", help="打包时额外生成 zip")
    p.add_argument("--num", type=int, default=None, help="交付序号（默认自动递增）")
    p.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    p.add_argument("--dry-run", action="store_true", help="仅打印计划；允许在任意宿主预览平台矩阵")
    return p


def build_targets(project, target_os):
    base = f"//chrome/browser/{project}"
    if project == "arupa_android":
        return [base + "/aar:arupa_kernel_aar", "//content/shell:pak"]
    targets = [base + ":arupa_kernel", "//content/shell:pak"]
    if target_os in ("win", "mac"):
        targets.append(base + ":render")
    if target_os == "win":
        targets.append(base + ":arupa_plugin_host")
    return targets


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
    if F.DRY_RUN:
        F.log("(dry-run) 将模块挂载和构建入口加入 src 的本地 Git exclude")
        return
    git_path = F.run(["git", "rev-parse", "--git-path", "info/exclude"], SRC, capture=True)
    exclude = Path(git_path)
    if not exclude.is_absolute():
        exclude = SRC / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    for path in paths:
        line = "/" + path
        if line not in text.splitlines():
            text = text.rstrip() + "\n" + line + "\n"
    write_if_changed(exclude, text)


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
    # root-pattern also avoids evaluating desktop-only test dependencies on Linux.
    graph = SRC / "arupa_build" / project / "BUILD.gn"
    lines = ['# Generated by scripts/build.py; do not edit.', 'group("delivery") {', '  testonly = true']
    if project == "arupa_desktop":
        lines += ['  deps = [ "//chrome/browser/arupa_desktop:arupa_kernel", "//content/shell:pak" ]',
                  '  if (is_win || is_mac) { deps += [ "//chrome/browser/arupa_desktop:render" ] }',
                  '  if (is_win) { deps += [ "//chrome/browser/arupa_desktop:arupa_plugin_host" ] }']
    else:
        lines += ['  deps = [ "//chrome/browser/arupa_android/aar:arupa_kernel_aar", "//content/shell:pak" ]']
    write_if_changed(graph, "\n".join(lines + ['}', '']))


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
    if args.plugin_runtime:
        command += ["--plugin-runtime", args.plugin_runtime.resolve()]
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
    is_browser = project.startswith("nomadbrowser.")
    is_android = project in ("arupa_android", "nomadbrowser.android")
    target_os = args.os or ("android" if is_android else F.HOST_OS)
    target_os = {"macos": "mac", "windows": "win"}.get(target_os, target_os)
    if target_os not in MATRIX or is_android != (target_os == "android"):
        p.error("桌面项目仅支持 win/mac/linux；Android 项目仅支持 android")
    machine = (os.environ.get("PROCESSOR_ARCHITEW6432") if F.HOST_OS == "win" else None) or platform.machine()
    host_cpu = {"aarch64": "arm64", "amd64": "x64", "x86_64": "x64", "i386": "x86", "i686": "x86"}.get(machine.lower(), machine.lower())
    arch = args.arch or ("all" if project == "arupa_android" else "arm64" if is_android else host_cpu)
    if arch != "all" and arch not in MATRIX[target_os]:
        p.error(f"{target_os} 支持的架构: {', '.join(MATRIX[target_os])}")
    actions = args.actions or ["all"]
    if set(actions) - {"gen", "build", "package", "all"}:
        p.error("动作只支持 gen / build / package / all；依赖下载请使用 fetch")
    actions = set(actions)
    if "all" in actions:
        actions = {"build", "package"} if is_browser else {"gen", "build", "package"}
    if args.link == "dynamic" and (is_browser or target_os == "android" or "package" in actions):
        p.error("Android、浏览器和交付打包仅支持 static；arupa_desktop dynamic 可执行 gen/build")
    if args.jobs < 1 or (args.num is not None and args.num < 1):
        p.error("--jobs / --num 必须大于 0")
    required_host = "linux" if target_os == "android" else target_os
    if not args.dry_run and F.HOST_OS != required_host:
        p.error(f"{target_os} 构建需要 {required_host} 宿主（当前 {F.HOST_OS}）")
    version = args.ver if is_browser and args.ver else F.chromium_version()
    if args.ver and args.ver != version:
        p.error(f"--ver {args.ver} 与 src/chrome/VERSION {version} 不一致，请先 fetch 指定版本")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        p.error("版本必须是 Chromium 四段版本号 X.Y.Z.W")
    arches = MATRIX[target_os] if arch == "all" else (arch,)
    if is_browser:
        import nomad
        nomad.run(ROOT, args, project, target_os, arches, version, actions)
        F.log("浏览器构建计划完成（未执行）" if args.dry_run else "浏览器构建流程完成")
        return 0
    if "package" in actions and not args.dry_run:
        runtime = args.plugin_runtime or ROOT / "plugin-runtime/nomad-plugin-runtime.js"
        if not runtime.is_file():
            F.err(f"缺少打包附件 {runtime}；用 --plugin-runtime 指定 nomad-plugin-runtime.js")
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
        config_path = args_template(target_os, args.args)
        templates = {cpu: render_args(config_path, target_os, cpu, args.link) for cpu in arches}
        native_tools.kernel_check(target_os, arches)
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
                F.run([gn, "gen", out, f"--root-target={root_target}", f"--root-pattern={root_target}"], SRC)
                write_if_changed(stamp, json.dumps(identity, indent=2) + "\n")
            elif "build" in actions and not args.dry_run:
                # args.gn 由本脚本以 UTF-8 写出（含中文注释），必须显式按 UTF-8 回读：
                # Windows 中文环境的默认编码是 GBK，回读时会直接抛 UnicodeDecodeError。
                if not stamp.exists() or json.loads(stamp.read_text(encoding="utf-8")) != identity \
                        or (out / "args.gn").read_text(encoding="utf-8") != template:
                    F.err(f"{out} 配置不匹配；请先执行 gen")
            if "build" in actions:
                F.run([ninja, "-C", out, "-j", str(args.jobs), *[t.removeprefix('//') for t in build_targets(project, target_os)]], SRC)
    if "package" in actions:
        # Android's packager combines both ABIs in one delivery when arch=all.
        package_arches = (arch,) if target_os == "android" else arches
        for cpu in package_arches:
            F.run(package_command(args, target_os, cpu, version), ROOT)
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
