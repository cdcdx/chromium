#!/usr/bin/env python3
"""Fetch depot_tools, pinned Chromium/DEPS, and four versioned Arupa/Nomad repositories."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
from platforms import MATRIX, normalize_os, architectures, host_for

if __name__ == "__main__":
    sys.modules["fetch"] = sys.modules[__name__]

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
CHROMIUM_SRC = WORKSPACE_ROOT / "src"
HOST_OS = {"Windows": "win", "Darwin": "mac", "Linux": "linux"}.get(platform.system(), "")
IS_WIN = HOST_OS == "win"
DRY_RUN = False
URL_DEPOT_TOOLS = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
URL_CHROMIUM = "https://chromium.googlesource.com/chromium/src.git"
# target -> (.env key prefix, checkout directory)
PROJECTS = {
    "arupa_desktop": ("arupa_desktop", "arupa_desktop"),
    "arupa_android": ("arupa_android", "arupa_android"),
    "nomadbrowser.pc": ("nomad_desktop", "nomadbrowser.pc"),
    "nomadbrowser.android": ("nomad_android", "nomadbrowser.android"),
}


def log(message):
    print(f"[INFO] {message}", flush=True)


def err(message):
    raise RuntimeError(message)


def load_config():
    cfg = {}
    path = WORKSPACE_ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


def cget(cfg, *keys, default=""):
    # An explicitly empty environment value overrides .env as well.
    for key in keys:
        for name in (key, key.upper()):
            if name in os.environ:
                return os.environ[name]
    for key in keys:
        if key in cfg:
            return cfg[key]
    return default


def apply_proxy(cfg, proxy=None):
    value = proxy if proxy is not None else cget(cfg, "https_proxy", "http_proxy")
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(key, None)
    if value:
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ[key] = value
    # Only affect child processes; never rewrite the user's global Git config.
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "http.proxy"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)
    return value


def run(cmd, cwd=None, capture=False):
    cmd = [str(item) for item in cmd]
    log(("(dry-run) " if DRY_RUN else "") + shlex.join(cmd))
    if DRY_RUN:
        return ""
    result = subprocess.run(cmd, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def resolve_depot_tools_dir(cfg):
    path = Path(cget(cfg, "depot_tools_dir", default="depot_tools")).expanduser()
    return path if path.is_absolute() else WORKSPACE_ROOT / path


def sanitize_library_path():
    """丢掉 LIBRARY_PATH 里不存在的目录。

    clang 会把 LIBRARY_PATH 的每一项原样变成 -L 交给链接器，而 ld64.lld 对不存在的
    目录报 "directory not found for option -L/usr/local/lib"；Chromium 的链接参数里有
    -Wl,-fatal_warnings（外加 -Werror），这条警告直接变成构建失败。库搜索路径本来就由
    GN 显式给出，这里只清理不存在的项，真实存在的目录原样保留。"""
    value = os.environ.get("LIBRARY_PATH", "")
    if not value:
        return
    items = [item for item in value.split(os.pathsep) if item]
    kept = [item for item in items if Path(item).is_dir()]
    if len(kept) == len(items):
        return
    if kept:
        os.environ["LIBRARY_PATH"] = os.pathsep.join(kept)
    else:
        os.environ.pop("LIBRARY_PATH", None)
    log(f"忽略 LIBRARY_PATH 里不存在的目录: {os.pathsep.join(i for i in items if i not in kept)}")


def ensure_real_python3(depot):
    """把真解释器挂到 PATH 最前面。

    macOS 的 /usr/bin/python3 只是个 xcrun 桩：它按 SDKROOT 反查真解释器。Chromium 给
    rust 构建脚本注入的 SDKROOT 指向 out/sdk/xcode_links/<sdk>（build/config/mac/mac_sdk.gni），
    xcodebuild 不认这个路径，于是所有 #!/usr/bin/env python3 的脚本（如
    build/toolchain/apple/linker_driver.py）都以 exit 72 起不来：
        xcode-select: Failed to locate 'python3', requesting installation of ...
    depot_tools 自举出的 python-bin/python3 是实打实的解释器，放在 PATH 最前面即可。"""
    if IS_WIN:
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    for directory in (depot / "python-bin", CHROMIUM_SRC / "third_party/cpython3/host/bin"):
        if not (directory / "python3").is_file() or str(directory) in current:
            continue
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        log(f"python3: {directory}（绕开系统 xcrun 桩）")
        return


def tool_environment(cfg):
    depot = resolve_depot_tools_dir(cfg)
    os.environ["PATH"] = str(depot) + os.pathsep + os.environ.get("PATH", "")
    os.environ["DEPOT_TOOLS_UPDATE"] = "0"
    os.environ["DEPOT_TOOLS_METRICS"] = "0"
    if IS_WIN:
        os.environ.setdefault("DEPOT_TOOLS_WIN_TOOLCHAIN", "0")
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        os.environ.pop(key, None)
    sanitize_library_path()
    ensure_real_python3(depot)
    return depot


def require_clean(path):
    if DRY_RUN:
        return
    dirty = run(["git", "status", "--porcelain", "--ignore-submodules=all"], path, capture=True)
    if dirty:
        err(f"{path} 有本地改动，已停止；请先提交或 stash（包括未跟踪文件）后重试。")


def setup_depot_tools(cfg):
    depot = resolve_depot_tools_dir(cfg)
    if (depot / ".git").exists():
        require_clean(depot)
        run(["git", "fetch", "origin", "HEAD"], depot)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], depot)
    elif depot.exists() and any(depot.iterdir()):
        err(f"{depot} 已存在且不是 Git 仓库")
    else:
        run(["git", "clone", cget(cfg, "depot_tools_src", default=URL_DEPOT_TOOLS), depot])
    tool_environment(cfg)
    # Run on every refresh: a marker from the previous revision does not prove
    # that the Python/CIPD packages match the newly fetched bootstrap manifest.
    if IS_WIN:
        run([depot / "bootstrap/win_tools.bat"], depot)
    else:
        run(["bash", "-c", 'source "$1" && bootstrap_python3', "bootstrap",
             depot / "bootstrap_python3"], depot)
    run([depot / ("gclient.bat" if IS_WIN else "gclient"), "--version"], WORKSPACE_ROOT)


def fetch_chromium(cfg, version, shallow):
    src = CHROMIUM_SRC
    url = cget(cfg, f"chromium_src_{HOST_OS}", "chromium_src", default=URL_CHROMIUM)
    if cget(cfg, "chromium_mirror", default="0") == "1":
        url = "https://github.com/chromium/chromium.git"
    if (src / ".git").exists():
        require_clean(src)
    elif src.exists() and any(src.iterdir()):
        err(f"{src} 已存在且不是 Git 仓库")
    else:
        run(["git", "init", src])
        run(["git", "remote", "add", "origin", url], src)
    flags = ["--depth=1"] if shallow else []
    if not shallow and (src / ".git").exists():
        if run(["git", "rev-parse", "--is-shallow-repository"], src, capture=True) == "true":
            flags = ["--unshallow"]
    # Fetch the exact tag into FETCH_HEAD; checkout never falls back to old HEAD.
    run(["git", "fetch", *flags, url, f"refs/tags/{version}:refs/tags/{version}"], src)
    run(["git", "checkout", "--detach", "FETCH_HEAD"], src)
    return url


def fetch_project(name, url, ref):
    """Check out a tag, branch, or commit without losing local changes."""
    dest = WORKSPACE_ROOT / PROJECTS[name][1]
    if (dest / ".git").exists():
        require_clean(dest)
    elif dest.exists() and any(dest.iterdir()):
        err(f"{dest} 已存在且不是 Git 仓库；请先核对、备份并移走，脚本不会覆盖")
    else:
        run(["git", "init", dest])
        run(["git", "remote", "add", "origin", url], dest)
    run(["git", "fetch", url, ref], dest)
    run(["git", "checkout", "--detach", "FETCH_HEAD"], dest)


def write_gclient(url, android, target_os=None):
    path = WORKSPACE_ROOT / ".gclient"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        # Preserve custom_vars/custom_deps and other user settings. The revision
        # is supplied explicitly to sync, so managed=False cannot silently drift.
        if not re.search(r'''["']name["']\s*:\s*["']src["']''', text):
            err("现有 .gclient 没有 src solution，请检查配置")
    else:
        text = ("solutions = [{\n"
                f"    'name': 'src', 'url': {url!r},\n"
                "    'deps_file': 'DEPS', 'managed': False,\n"
                "    'custom_deps': {}, 'custom_vars': {},\n"
                "}]\n")
    requested_os = "android" if android else (target_os or HOST_OS)
    match = re.search(r"(?m)^target_os\s*=\s*\[(.*?)\]", text, re.S)
    values = re.findall(r"['\"]([^'\"]+)['\"]", match[1]) if match else []
    # Retain host tools even when target_os_only is configured by the user.
    merged_os = values + [value for value in dict.fromkeys((HOST_OS, requested_os))
                          if value and value not in values]
    replacement = "target_os = " + repr(merged_os)
    text = (text[:match.start()] + replacement + text[match.end():] if match
            else text.rstrip() + "\n" + replacement + "\n")
    # Fetch all CPUs promised by the build entrypoint, including ARM64 sysroots.
    match = re.search(r"(?m)^target_cpu\s*=\s*\[(.*?)\]", text, re.S)
    cpus = re.findall(r"['\"]([^'\"]+)['\"]", match[1]) if match else []
    merged = cpus + [cpu for cpu in ("x86", "x64", "arm64") if cpu not in cpus]
    line = "target_cpu = " + repr(merged)
    text = text[:match.start()] + line + text[match.end():] if match else text.rstrip() + "\n" + line + "\n"
    log(f"配置 {path}")
    if not DRY_RUN:
        path.write_text(text, encoding="utf-8")


def sync_deps(cfg, version, android, shallow, nohooks, jobs, target_os=None):
    depot = tool_environment(cfg)
    gclient = depot / ("gclient.bat" if IS_WIN else "gclient")
    if not DRY_RUN and not gclient.is_file():
        err("缺少 depot_tools，请先执行 fetch 的默认流程")
    if not DRY_RUN:
        head = run(["git", "rev-parse", "HEAD"], CHROMIUM_SRC, capture=True)
        actual = chromium_version()
        if actual != version:
            err(f"src 版本是 {actual}，目标是 {version}；请先运行 fetch chromium --ver {version}")
    else:
        head = f"refs/tags/{version}"
    write_gclient(cget(cfg, "chromium_src", default=URL_CHROMIUM), android, target_os)
    cmd = [gclient, "sync", "--revision", f"src@{head}", "--jobs", str(jobs)]
    if shallow:
        cmd += ["--no-history", "--shallow"]
    if nohooks:
        cmd.append("--nohooks")
    run(cmd, WORKSPACE_ROOT)


def chromium_version():
    path = CHROMIUM_SRC / "chrome/VERSION"
    if not path.is_file():
        err(f"缺少 {path}，请先 fetch")
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return ".".join(values[key].strip() for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))


def build_parser():
    p = argparse.ArgumentParser(description="更新 depot_tools 及工具链；拉取指定版本 Chromium、DEPS 和四个业务仓库")
    p.add_argument("targets", nargs="*", help="log / branch（查看当前一级子目录中仓库的最新提交/本地分支）；all（默认）/ depot_tools / chromium / deps / hooks / android / update / toolchains / host-deps / sysroots / metal / dotnet / android-sdk / jdk / " + " / ".join(PROJECTS))
    for name, (prefix, _) in PROJECTS.items():
        option = prefix.replace("_", "-")
        p.add_argument(f"--{option}-src", dest=prefix + "_src", default=None, help=f"{name} Git 地址")
        p.add_argument(f"--{option}-ver", dest=prefix + "_ver", default=None, help=f"{name} tag / branch / commit")
    p.add_argument("--ver", default="", help="Chromium X.Y.Z.W；默认 .env chromium_ver")
    p.add_argument("--os", type=normalize_os, choices=tuple(MATRIX), default=HOST_OS)
    p.add_argument("--arch", choices=("x86", "x64", "arm64", "all"), default="all", help="工具链准备的目标架构，默认 all")
    p.add_argument("--install-host-deps", action="store_true", help="显式安装宿主依赖；Linux 可能需要 sudo，Windows 需官方 VS 安装器")
    p.add_argument("--vs-installer", type=Path, help="Windows 官方 Visual Studio bootstrapper 路径（用于 host-deps）")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--proxy", default=None)
    p.add_argument("--nohooks", "--no-hooks", action="store_true")
    history = p.add_mutually_exclusive_group()
    history.add_argument("--shallow", dest="shallow", action="store_true")
    history.add_argument("--full-history", dest="shallow", action="store_false")
    p.set_defaults(shallow=True)
    p.add_argument("--save", action="store_true", help="成功后将 --ver 保存到 .env")
    p.add_argument("--dry-run", action="store_true", help="仅打印计划，不写文件、不联网")
    return p


def main(argv=None):
    global DRY_RUN
    parser = build_parser()
    args = parser.parse_args(argv)
    DRY_RUN = args.dry_run
    requested = args.targets or ["all"]
    if set(requested) & {"log", "branch"}:
        if set(requested) - {"log", "branch"} or args.save or args.install_host_deps or args.vs_installer:
            parser.error("log / branch 是只读命令，不能与拉取、保存或安装动作混用")
        import repositories
        return repositories.show(Path.cwd(), list(dict.fromkeys(requested)), args.dry_run)
    cfg = load_config()
    if "android" in requested:
        args.os = "android"
    import toolchains
    valid = {"all", "update", "depot_tools", "chromium", "deps", "hooks", "android", "toolchains", "host-deps", "sysroots", *toolchains.TARGETS, *PROJECTS}
    if set(requested) - valid:
        parser.error("未知目标: " + ", ".join(sorted(set(requested) - valid)))
    targets = set()
    for name in requested:
        if name == "all":
            targets.update({"depot_tools", "chromium", "deps", *PROJECTS})
        elif name in ("update", "android"):
            targets.update({"depot_tools", "chromium", "deps"})
        elif name == "toolchains":
            targets.update({"depot_tools", "deps"})
            if args.os == "android":
                targets.add("android-sdk")
            elif args.os == "mac" and HOST_OS == "mac":
                targets.add("metal")
        else:
            targets.add(name)
    if args.install_host_deps:
        targets.add("host-deps")
    if args.vs_installer and (args.os != "win" or "host-deps" not in targets):
        parser.error("--vs-installer 仅用于 --os win 的 host-deps / --install-host-deps")
    if targets & {"host-deps", "sysroots"} or "toolchains" in requested:
        required_host = host_for(args.os)
        if not DRY_RUN and HOST_OS != required_host:
            parser.error(f"{args.os} 工具链准备需要 {required_host} 宿主")
        if "sysroots" in targets and args.os != "linux":
            parser.error("sysroots 仅适用于 --os linux")
    try:
        arches = architectures(args.os, args.arch)
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    if args.nohooks and ("toolchains" in requested or targets & {"android-sdk", "jdk"}):
        parser.error("工具链准备需要 hooks；仅下载依赖请使用 fetch deps --nohooks")
    for tool, required_host in (("metal", "mac"), ("android-sdk", "linux"), ("jdk", "linux")):
        if tool in targets and not DRY_RUN and HOST_OS != required_host:
            parser.error(f"{tool} 需要 {required_host} 宿主")
    version = args.ver or cget(cfg, "chromium_ver", "CHROMIUM_VERSION")
    if targets & {"chromium", "deps"} and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        parser.error("请用 --ver 或 .env chromium_ver 指定 Chromium 四段版本号")
    if args.jobs < 1:
        parser.error("--jobs 必须大于 0")
    if args.save and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", args.ver):
        parser.error("--save 需要显式 --ver X.Y.Z.W")
    repositories = {}
    for name, (prefix, _) in PROJECTS.items():
        if name not in targets:
            continue
        url = getattr(args, prefix + "_src")
        ref = getattr(args, prefix + "_ver")
        url = cget(cfg, prefix + "_src") if url is None else url
        ref = cget(cfg, prefix + "_ver") if ref is None else ref
        if not url or not ref or ref.startswith("-"):
            parser.error(f"{name} 需要配置 {prefix}_src 和 {prefix}_ver（.env 或命令行）")
        repositories[name] = (url, ref)
    apply_proxy(cfg, args.proxy)
    android = args.os == "android"
    if "depot_tools" in targets:
        setup_depot_tools(cfg)
    if "chromium" in targets:
        url = fetch_chromium(cfg, version, args.shallow)
        write_gclient(url, android, args.os)
    import native_tools
    if "host-deps" in targets:
        native_tools.setup_host(args.os, arches, args.vs_installer)
    if "deps" in targets:
        sync_deps(cfg, version, android, args.shallow, args.nohooks, args.jobs, args.os)
    if "hooks" in targets and not targets & {"android-sdk", "jdk"}:
        depot = tool_environment(cfg)
        run([depot / ("gclient.bat" if IS_WIN else "gclient"), "runhooks"], WORKSPACE_ROOT)
    for name, (url, ref) in repositories.items():
        fetch_project(name, url, ref)
    for name in ("metal", "dotnet", "jdk", "android-sdk"):
        if name in targets:
            toolchains.setup(name, cfg)
    if "sysroots" in targets or ("toolchains" in requested and args.os == "linux"):
        native_tools.setup_sysroots(arches)
    if "toolchains" in requested:
        native_tools.kernel_check(args.os, arches)
    if args.save:
        path = WORKSPACE_ROOT / ".env"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        line = f"chromium_ver={args.ver}"
        text = re.sub(r"(?m)^chromium_ver=.*$", line, text) if re.search(r"(?m)^chromium_ver=", text) else text.rstrip() + "\n" + line + "\n"
        if not DRY_RUN:
            path.write_text(text, encoding="utf-8")
    log("拉取流程完成" if not DRY_RUN else "拉取计划完成（未执行）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
