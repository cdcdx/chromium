#!/usr/bin/env python3
"""Fetch depot_tools, pinned Chromium/DEPS, and four versioned Arupa/Nomad repositories."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import random
import re
import shlex
import subprocess
import sys
import threading
import time
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
# target -> (.env key prefix, checkout directory)；两者的名字当前总是同一个，
# 用名字元组派生，避免同一串标识符写三遍后改名时漏改。
PROJECT_NAMES = ("arupa_desktop", "arupa_android", "nomad_desktop", "nomad_android")
PROJECTS = {name: (name, name) for name in PROJECT_NAMES}
# 瞬时网络故障才重试：googlesource 对匿名共享配额限流时返回 429 / RESOURCE_EXHAUSTED，
# 以及常见的 DNS/连接抖动。参数错误、引用不存在等确定性失败不在其中，重试没有意义。
TRANSIENT_NETWORK = re.compile(
    r"RESOURCE_EXHAUSTED|rate limit|too many requests"
    r"|returned error: (?:429|5\d\d)|HTTP (?:429|5\d\d)"
    r"|RPC failed|early EOF|remote end hung up|unexpected disconnect"
    r"|(?:Could not|Couldn't|unable to) resolve host|Temporary failure in name resolution"
    r"|Connection (?:reset|timed out|refused)|Operation timed out"
    r"|ETIMEDOUT|ECONNRESET|ECONNREFUSED|EAI_AGAIN|ENOTFOUND"
    r"|socket hang up|Client network socket disconnected|npm error network"
    r"|gnutls_handshake\(\) failed|SSL_ERROR", re.I)
RATE_LIMITED = re.compile(
    r"RESOURCE_EXHAUSTED|rate limit|too many requests|returned error: 429|HTTP 429", re.I)
DEFAULT_RETRIES = 5
DEFAULT_RETRY_DELAY = 10.0
MAX_RETRY_DELAY = 120.0
# 超过这个时长没有输出就打印一次“仍在运行”，避免把静默下载误判成卡死。
HEARTBEAT_SECONDS = 120.0
# 补拉空 DEPS 仓库时的重试次数：这类仓库都不小，一次断流就得重下。
DEPS_REPAIR_RETRIES = 8
# depot_tools 自举生成、而它自己又没 gitignore 的路径；会被 require_clean 误判成"本地改动"。
DEPOT_GENERATED = ("python-bin", "python3_bin_reldir.txt", "bootstrap-*_bin")
RETRIES = DEFAULT_RETRIES
RETRY_DELAY = DEFAULT_RETRY_DELAY


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


def retry_wait(attempt):
    """指数退避并加入抖动，避免多个失败请求在同一时刻一起重试。"""
    return min(RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY) + random.uniform(0, RETRY_DELAY / 2)


def pump(stream, sink, chunks, state=None):
    """分块转发子进程管道并留档。

    分块（而不是按行）转发才能让 git --progress 的 \\r 进度行保持实时；每条管道一个
    线程，两个管道同时读，不会因为写满而死锁。"""
    while True:
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            return
        text = chunk.decode("utf-8", "replace")
        chunks.append(text)
        if state is not None:
            state["last"] = time.time()
        sink.write(text)
        sink.flush()


def heartbeat(state):
    """长时间没有输出时周期性提示。

    gclient/vpython/CIPD 这类步骤会把子进程输出缓存到自己结束才打印；没有心跳时，
    静默的慢速下载（例如 vpython 从 Google Artifact Registry 拉 wheel）看起来就像卡死。"""
    while not state["done"].wait(HEARTBEAT_SECONDS):
        idle = time.time() - state["last"]
        if idle >= HEARTBEAT_SECONDS:
            log(f"…仍在运行（已 {int((time.time() - state['start']) / 60)} 分钟，"
                f"最近 {int(idle)}s 无输出）：{state['label']}")


def execute(cmd, cwd, capture, tee):
    """执行命令，返回 (stdout, returncode, stderr)。

    tee 为真时 stdout/stderr 都一边实时转发一边留档：既可读输出判断失败是否属于可重试
    的限流/网络抖动，也不丢实时进度。"""
    if capture or not tee:
        result = subprocess.run(cmd, cwd=cwd, check=False, text=True, encoding="utf-8",
                                errors="replace", stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if tee else None)
        message = result.stderr or ""
        if message:
            sys.stderr.write(message)
            sys.stderr.flush()
        return (result.stdout or "").strip() if capture else "", result.returncode, message
    with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        stdout_chunks, stderr_chunks = [], []
        state = {"start": time.time(), "last": time.time(), "done": threading.Event(),
                 "label": shlex.join(cmd)[:160]}
        threads = [threading.Thread(target=pump, args=(process.stdout, sys.stdout, stdout_chunks, state), daemon=True),
                   threading.Thread(target=pump, args=(process.stderr, sys.stderr, stderr_chunks, state), daemon=True),
                   threading.Thread(target=heartbeat, args=(state,), daemon=True)]
        for thread in threads:
            thread.start()
        try:
            for thread in threads[:2]:
                thread.join()
            process.wait()
        finally:
            state["done"].set()
    return "".join(stdout_chunks).strip(), process.returncode, "".join(stderr_chunks)


def run(cmd, cwd=None, capture=False, retry=False, tee=False, retries=None):
    """执行命令；retry 为真时对瞬时网络错误（含 googlesource 429 限流）退避重试。

    retries 可覆盖默认重试次数（大仓补拉这类一次要下几百 MB 的操作用得着）。
    tee 为真时 stdout/stderr 一边实时转发一边留档，失败时 stderr 随 CalledProcessError 返回
    （gclient 这类把错误打到 stdout 的命令，两条流都会参与重试判断）。"""
    cmd = [str(item) for item in cmd]
    log(("(dry-run) " if DRY_RUN else "") + shlex.join(cmd))
    if DRY_RUN:
        return ""
    attempts = max(0, int(RETRIES if retries is None else retries)) if retry else 0
    for attempt in range(attempts + 1):
        stdout, code, message = execute(cmd, cwd, capture, tee=tee or attempts > 0)
        if code == 0:
            return stdout if capture else ""
        output = "\n".join(part for part in (message, stdout) if part)
        if attempt < attempts and TRANSIENT_NETWORK.search(output):
            reason = "远端限流（HTTP 429）" if RATE_LIMITED.search(output) else "网络瞬时失败"
            wait = retry_wait(attempt)
            log(f"{reason}，{wait:.1f}s 后进行第 {attempt + 2}/{attempts + 1} 次尝试")
            time.sleep(wait)
            continue
        if attempts and RATE_LIMITED.search(output):
            err("远端限流（HTTP 429）且连续 "
                f"{attempt + 1} 次尝试均失败：Chromium googlesource 对匿名共享配额的短时限流是常见原因。"
                "请稍等几分钟后重跑（本地 tag 和已下载对象会复用，不会重复下载）；"
                "拉取 Chromium 可在 .env 设置 chromium_mirror=1 改用 GitHub 镜像；"
                "也可设置 https_proxy 更换出口 IP 后重试。")
        raise subprocess.CalledProcessError(code, cmd, output=stdout, stderr=message)
    return ""


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


def python3_candidates(depot):
    """真解释器候选目录，按优先级返回。

    depot_tools 新自举布局把解释器放在 bootstrap-*_bin/python3/bin，目录名记在
    python3_bin_reldir.txt；旧布局是 python-bin。最后是 Chromium 自带的 cpython3。"""
    candidates = []
    reldir = depot / "python3_bin_reldir.txt"
    if reldir.is_file():
        relative = reldir.read_text(encoding="utf-8", errors="replace").strip()
        if relative:
            candidates.append(depot / relative)
    candidates += [depot / "python-bin", CHROMIUM_SRC / "third_party/cpython3/host/bin"]
    return candidates


def ensure_python_alias(directory):
    """保证 `python` 能解析到 directory 里的 python3，返回提供 `python` 的目录。

    depot_tools 的包装脚本（gclient、gsutil.py、ensure_bootstrap…）执行的是 `python`，
    而新自举目录里只有 `python3`，缺失时直接 `exec: python: not found`（exit 127）。
    先在同一目录补软链；目录不可写（例如系统 python 目录）就退到工作区 .tools/bin。"""
    if IS_WIN:
        return directory
    alias = directory / "python"
    if os.path.lexists(alias):
        return directory
    try:
        alias.symlink_to("python3")
        return directory
    except OSError:
        pass
    shim = WORKSPACE_ROOT / ".tools/bin"
    link = shim / "python"
    try:
        shim.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(link):
            link.symlink_to(directory / "python3")
        return shim
    except OSError as error:
        log(f"警告: 无法提供 python 命令（{error}）；depot_tools 的 gclient 等脚本可能起不来")
        return None


def ensure_real_python3(depot):
    """把真解释器挂到 PATH 最前面，并保证 `python` 可用。

    macOS 的 /usr/bin/python3 只是个 xcrun 桩：它按 SDKROOT 反查真解释器。Chromium 给
    rust 构建脚本注入的 SDKROOT 指向 out/sdk/xcode_links/<sdk>（build/config/mac/mac_sdk.gni），
    xcodebuild 不认这个路径，于是所有 #!/usr/bin/env python3 的脚本（如
    build/toolchain/apple/linker_driver.py）都以 exit 72 起不来：
        xcode-select: Failed to locate 'python3', requesting installation of ...
    depot_tools 自举出的解释器是实打实的，放在 PATH 最前面即可。"""
    if IS_WIN:
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    for directory in python3_candidates(depot):
        if not (directory / "python3").is_file():
            continue
        provider = ensure_python_alias(directory)
        for entry in (str(directory), str(provider) if provider else ""):
            if entry and entry not in current:
                os.environ["PATH"] = entry + os.pathsep + os.environ.get("PATH", "")
                current.append(entry)
        log(f"python3: {directory}（{'绕开系统 xcrun 桩' if HOST_OS == 'mac' else 'depot_tools 自举解释器'}）")
        return


def tool_environment(cfg):
    depot = resolve_depot_tools_dir(cfg)
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(depot) not in entries:  # 可重复调用（sync/hooks/工具链各自会调一次）
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


def exclude_paths(repo, paths, description):
    """把生成物写进 <repo>/.git/info/exclude，避免被 require_clean 当成"本地改动"。

    只影响本仓库的状态显示：不写 .gitignore（会变成需要提交的改动），也不碰全局 Git 配置。"""
    if DRY_RUN:
        log(f"(dry-run) 将{description}加入 {repo} 的本地 Git exclude")
        return
    relative = run(["git", "rev-parse", "--git-path", "info/exclude"], repo, capture=True)
    exclude = Path(relative)
    if not exclude.is_absolute():
        exclude = repo / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = text.splitlines()
    changed = False
    for path in paths:
        line = "/" + str(path)
        if line not in lines:
            text = text.rstrip() + "\n" + line + "\n"
            lines.append(line)
            changed = True
    if not changed:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(text, encoding="utf-8")
    log(f"已把{description}加入 {exclude}")


def require_clean(path):
    if DRY_RUN:
        return
    dirty = run(["git", "status", "--porcelain", "--ignore-submodules=all"], path, capture=True)
    if not dirty:
        return
    entries = dirty.splitlines()
    listing = "\n".join(f"  {entry}" for entry in entries[:10])
    if len(entries) > 10:
        listing += f"\n  …另有 {len(entries) - 10} 项"
    err(f"{path} 有本地改动，已停止；请先提交或 stash（包括未跟踪文件）后重试:\n{listing}")


def setup_depot_tools(cfg):
    depot = resolve_depot_tools_dir(cfg)
    if (depot / ".git").exists():
        # depot_tools 自举会生成未跟踪的 python-bin / bootstrap-*_bin / python3_bin_reldir.txt；
        # 它们是工具自己的产物，不该让 require_clean 判定成"本地改动"而永久挡住更新。
        exclude_paths(depot, DEPOT_GENERATED, "depot_tools 自举产物")
        require_clean(depot)
        run(["git", "fetch", "--progress", "origin", "HEAD"], depot, retry=True)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], depot)
    elif depot.exists() and any(depot.iterdir()):
        err(f"{depot} 已存在且不是 Git 仓库")
    else:
        run(["git", "clone", "--progress", cget(cfg, "depot_tools_src", default=URL_DEPOT_TOOLS), depot],
            retry=True)
    tool_environment(cfg)
    # Run on every refresh: a marker from the previous revision does not prove
    # that the Python/CIPD packages match the newly fetched bootstrap manifest.
    # 新版 depot_tools 去掉了 bootstrap_python3，改由 ensure_bootstrap 同步 CIPD/Python。
    if IS_WIN:
        run([depot / "bootstrap/win_tools.bat"], depot)
    elif (depot / "bootstrap_python3").is_file():
        run(["bash", "-c", 'source "$1" && bootstrap_python3', "bootstrap",
             depot / "bootstrap_python3"], depot)
    elif (depot / "ensure_bootstrap").is_file():
        run(["bash", depot / "ensure_bootstrap"], depot)
    else:
        log("警告: depot_tools 里既没有 bootstrap_python3 也没有 ensure_bootstrap，跳过解释器自举")
    # 自举可能刚刚建好解释器目录：重新解析一次 PATH，否则 gclient 的 exec python 会落空。
    tool_environment(cfg)
    run([depot / ("gclient.bat" if IS_WIN else "gclient"), "--version"], WORKSPACE_ROOT)


def local_tag(src, ref):
    """本地已有该 tag 且对象完整时无需再次联网（Chromium tag 不会变化）。"""
    try:
        run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], src, capture=True)
    except subprocess.CalledProcessError:
        return False
    return True


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
    tag = f"refs/tags/{version}"
    # Fetch the exact tag into the local tag ref; checkout never falls back to old HEAD.
    # A tag already present locally means the objects are complete, so a re-run stays
    # offline; --unshallow still has to reach the network.
    if DRY_RUN or flags == ["--unshallow"] or not local_tag(src, tag):
        run(["git", "fetch", "--progress", *flags, url, f"{tag}:{tag}"], src, retry=True)
    else:
        log(f"本地已有 {tag}，跳过重复下载")
    run(["git", "checkout", "--detach", tag], src)
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
    run(["git", "fetch", "--progress", url, ref], dest, retry=True)
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


DEPS_ENTRY = re.compile(r"^\s*'([^']+)':\s*'([^']+)',?\s*$", re.M)


def deps_entries():
    """读 gclient 记录的 DEPS 清单，返回 [(相对路径, url, revision)]；没有清单返回 None。"""
    path = WORKSPACE_ROOT / ".gclient_entries"
    if not path.is_file():
        return None
    entries = []
    for relative, url in DEPS_ENTRY.findall(path.read_text(encoding="utf-8", errors="replace")):
        entries.append((relative, *url.rsplit("@", 1)) if "@" in url else (relative, url, ""))
    return entries


def unsynced_deps():
    """已建 git 仓库但没有提交的依赖：gclient 中断后的残骸。返回 None 表示从未同步过。

    只检查自带 .git 的独立仓库（167 个约 0.15s）；gs:// 与 CIPD 条目缺了不影响内核构建。"""
    entries = deps_entries()
    if entries is None:
        return None
    broken = []
    for relative, url, _ in entries:
        path = WORKSPACE_ROOT / relative
        if not (path / ".git").exists():
            continue
        probe = subprocess.run(["git", "--no-optional-locks", "-C", str(path), "rev-parse",
                                "--verify", "--quiet", "HEAD"], capture_output=True)
        if probe.returncode:
            broken.append((relative, url))
    return broken


def repair_deps():
    """补拉 gclient 留在空状态的 DEPS 仓库，返回补拉成功的相对路径列表。

    gclient sync 遇到网络抖动时会把某些仓库留在「只 init + 配好 origin」的状态，却仍报告
    100% 完成；之后 GN 只会报 `Unable to load "//third_party/.../BUILD.gn"`，看不出原因。
    这里按清单里钉住的 revision 逐个补拉（与 gclient 一致用 --depth=1 浅拉取）并检出。"""
    if DRY_RUN:
        return []
    broken = unsynced_deps()
    if broken is None:
        err(f"找不到 {WORKSPACE_ROOT / '.gclient_entries'}：gclient sync 从未成功过，"
            "请先执行 bash fetch.sh chromium")
    if not broken:
        return []
    entries = {relative: (url, revision) for relative, url, revision in deps_entries()}
    log(f"{len(broken)} 个 DEPS 仓库未同步完成（空仓库），逐个补拉")
    repaired, failed = [], []
    for relative, url in broken:
        path = WORKSPACE_ROOT / relative
        url, revision = entries.get(relative, (url, ""))
        if not revision:
            failed.append(relative)
            continue
        # 中断留下的 tmp_pack_* 永远不会被 git 使用，清掉避免白占空间。
        for stale in (path / ".git/objects/pack").glob("tmp_pack_*"):
            stale.unlink(missing_ok=True)
        log(f"补拉 {relative} @ {revision[:12]}")
        try:
            run(["git", "-C", str(path), "fetch", "--depth=1", "--no-tags", "--progress", url, revision],
                retry=True)
            run(["git", "-C", str(path), "checkout", "--detach", "FETCH_HEAD"])
        except subprocess.CalledProcessError as error:
            failed.append(f"{relative}（退出码 {error.returncode}）")
            continue
        repaired.append(relative)
    if failed:
        err("以下 DEPS 仓库补拉失败:\n  " + "\n  ".join(failed) + "\n请稍后重跑: bash fetch.sh deps")
    return repaired


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
    # gclient 自己会并发拉上百个 googlesource 仓库，是最容易撞 429 的一步；
    # 失败后可从断点续传（已完成的仓库不会重下），因此按瞬时故障重试。
    run(cmd, WORKSPACE_ROOT, retry=True)
    repaired = repair_deps()
    if repaired:
        log(f"已补拉 {len(repaired)} 个 DEPS 仓库: " + ", ".join(repaired))


def chromium_version():
    path = CHROMIUM_SRC / "chrome/VERSION"
    if not path.is_file():
        err(f"缺少 {path}，请先 fetch")
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return ".".join(values[key].strip() for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))


def build_parser():
    p = argparse.ArgumentParser(description="更新 depot_tools 及工具链；拉取指定版本 Chromium、DEPS 和四个业务仓库")
    p.add_argument("targets", nargs="*", help="pull / push（同步当前一级子目录仓库的当前分支 upstream）；log / branch（查看最新提交/本地分支）；all（默认）/ depot_tools / chromium / deps / hooks / android / update / toolchains / host-deps / sysroots / metal / dotnet / android-sdk / jdk / " + " / ".join(PROJECTS))
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
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                   help=f"瞬时网络错误（含 googlesource 429 限流）的重试次数，默认 {DEFAULT_RETRIES}，0 关闭")
    p.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY,
                   help=f"首次重试前等待秒数，之后指数退避，默认 {DEFAULT_RETRY_DELAY:g}")
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
    global DRY_RUN, RETRIES, RETRY_DELAY
    parser = build_parser()
    args = parser.parse_args(argv)
    DRY_RUN = args.dry_run
    requested = args.targets or ["all"]
    if set(requested) & {"pull", "push"}:
        if len(requested) != 1 or args.save or args.install_host_deps or args.vs_installer:
            parser.error("pull / push 必须单独执行，不能与其他动作混用")
        import repositories
        return repositories.sync(Path.cwd(), requested[0], args.dry_run)
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
    if args.retries < 0 or args.retry_delay < 0:
        parser.error("--retries 与 --retry-delay 不能为负数")
    RETRIES, RETRY_DELAY = args.retries, args.retry_delay
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
        run([depot / ("gclient.bat" if IS_WIN else "gclient"), "runhooks"], WORKSPACE_ROOT, retry=True)
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
