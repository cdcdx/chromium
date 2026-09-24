#!/usr/bin/env python3
# =============================================================================
#  工作区源码 / 依赖拉取更新 —— Windows / macOS / Linux 统一入口
#
#  用法: python3 scripts/fetch.py [目标...] [选项]
#    目标（可多选，默认 all）:
#      depot_tools   depot_tools 工具集（gclient / gn / autoninja）
#      chromium      src/，pin 到 --ver（默认浅克隆，只拉一个版本）
#      v8            src/v8（通常由 DEPS 带出，需单独拉时才用）
#      deps          gclient sync（第三方依赖 + hooks）
#      hooks         gclient runhooks（只补工具链；配 deps --nohooks 用）
#      android       Android 依赖（.gclient 加 target_os=android 后 sync；宿主须 Linux）
#      sysdeps       系统级依赖（Linux: install-build-deps.sh；mac/win: 体检提示）
#      kernel        nomadbrowser.kernel/
#      pc            nomadbrowser.pc/
#      link          内核挂载到 src/chrome/browser/arupa_desktop
#      all           depot_tools + chromium + v8 + kernel + pc + link（默认）
#    选项:
#      --ver X.Y.Z.W      版本标签（默认取 .env chromium_ver）
#      --proxy URL        HTTP 代理（默认取 .env https_proxy）
#      --jobs N           gclient 并行度（默认 8）
#      --nohooks          deps 只同步依赖，不跑 hooks
#      --shallow          浅克隆 / gclient --no-history（默认开）
#      --full-history     拉全历史（关掉浅克隆）
#      --force            强制 sync / 覆盖 .gclient
#      --no-link          all 时跳过挂载
#      -y, --yes          非交互
#      --dry-run          只打印命令，不执行
#      -h, --help
#
#  配置: .env（键名见 .env.example）；已 export 的同名环境变量优先。
#  幂等: 目录已存在时只 fetch + 切目标 ref；工作区脏则保留 HEAD（绝不丢改动）。
#
#  典型流程:
#      python3 scripts/fetch.py --sysdeps          # Linux 首次：系统依赖
#      python3 scripts/fetch.py                    # 拉 5 份源码 + 挂载
#      python3 scripts/fetch.py deps               # gclient sync + hooks
#      python3 scripts/fetch.py deps --nohooks && python3 scripts/fetch.py hooks
#      python3 scripts/fetch.py android            # Android 依赖
# =============================================================================
from __future__ import annotations

import argparse
import atexit
import getpass
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
CHROMIUM_SRC = WORKSPACE_ROOT / "src"
V8_SRC = CHROMIUM_SRC / "v8"
KERNEL_DIR = WORKSPACE_ROOT / "nomadbrowser.kernel"
PC_DIR = WORKSPACE_ROOT / "nomadbrowser.pc"
MODULE_RELDIR = "chrome/browser/arupa_desktop"
GCLIENT_FILE = WORKSPACE_ROOT / ".gclient"
STATE_DIR = WORKSPACE_ROOT / ".fetch"

URL_DEPOT_TOOLS = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
URL_CHROMIUM = "https://chromium.googlesource.com/chromium/src.git"
URL_CHROMIUM_MIRROR = "https://github.com/chromium/chromium.git"  # 坑见 chromium_url()

HOST = platform.system()                              # Windows / Darwin / Linux
HOST_OS = {"Windows": "win", "Darwin": "mac", "Linux": "linux"}.get(HOST, "linux")
IS_WIN = HOST == "Windows"

USE_COLOR = not IS_WIN and sys.stdout.isatty()
DRY_RUN = False
YES = False
_ASKPASS = ""


# ── 输出 ────────────────────────────────────────────────────────────────────
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def log(msg):   print(_c("1;34", "[INFO] ") + str(msg), flush=True)
def warn(msg):  print(_c("1;33", "[WARN] ") + str(msg), file=sys.stderr, flush=True)
def step(msg):  print("\n" + _c("1;36", f"===== {msg} ====="), flush=True)


def err(msg):
    print(_c("1;31", "[ERROR] ") + str(msg), file=sys.stderr, flush=True)
    sys.exit(1)


def out(cmd, cwd=None) -> str:
    """取命令 stdout；失败返回空串。"""
    cmd = [str(c) for c in cmd]
    if DRY_RUN:
        log("(dry-run) " + " ".join(shlex.quote(c) for c in cmd))
        return ""
    try:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except OSError:
        return ""


def run(cmd, cwd=None, check=True) -> bool:
    """执行命令；返回是否成功（check=False 时不抛异常）。"""
    cmd = [str(c) for c in cmd]
    if DRY_RUN:
        log("(dry-run) " + " ".join(shlex.quote(c) for c in cmd))
        return True
    try:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    except OSError as e:
        if check:
            err(f"命令无法执行: {cmd[0]} ({e})")
        return False
    if r.returncode != 0 and check:
        err(f"命令失败（exit {r.returncode}）: {' '.join(shlex.quote(c) for c in cmd)}")
    return r.returncode == 0


def git(args, cwd=None, check=True, capture=False):
    if capture:
        return out(["git", *args], cwd=cwd)
    return run(["git", *args], cwd=cwd, check=check)


# ── 配置 ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    """读 .env；已 export 的同名环境变量优先。"""
    cfg: dict = {}
    f = WORKSPACE_ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                cfg.setdefault(k, v)
        log(f"已加载 .env: {f}")
    else:
        warn(f"未找到 .env（{f}）—— 走默认值/外部环境变量")
    return cfg


def cget(cfg: dict, *keys, default: str = "") -> str:
    for k in keys:
        v = cfg.get(k) or os.environ.get(k) or os.environ.get(k.upper())
        if v:
            return v
    return default


def chromium_url(cfg: dict, proxy: str) -> str:
    """googlesource 经代理约 120 秒被对端切断（浅克隆/取 tag 时必现），
    未显式指定镜像开关时自动换 GitHub 镜像——commit 与上游 tag 一致。"""
    url = cget(cfg, f"chromium_src_{HOST_OS}", "chromium_src", default=URL_CHROMIUM)
    mirror = cget(cfg, "chromium_mirror", default="auto")
    if mirror == "auto" and proxy and "googlesource" in url:
        warn(f"检测到 googlesource 源 + 代理 —— 改用 GitHub 镜像（同一 commit）: {URL_CHROMIUM_MIRROR}")
        return URL_CHROMIUM_MIRROR
    if mirror == "1":
        return URL_CHROMIUM_MIRROR
    return url


def apply_proxy(cfg: dict, proxy: str):
    p = proxy or cget(cfg, "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
    if not p:
        warn("未配置代理，直连（拉 chromium / cipd 通常需要在 .env 设 https_proxy）")
        return ""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ[k] = p
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1,.local"
    log(f"代理: {p}")
    if not out(["git", "config", "--global", "--get", "http.proxy"]):
        git(["config", "--global", "http.proxy", p], check=False)
        git(["config", "--global", "https.proxy", p], check=False)
    return p


# ── depot_tools ─────────────────────────────────────────────────────────────
def resolve_depot_tools_dir(cfg: dict) -> Path:
    """优先级: .env depot_tools_dir > <工作区>/depot_tools > ~/depot_tools"""
    d = cget(cfg, "depot_tools_dir", "DEPOT_TOOLS_DIR")
    if d:
        p = Path(d).expanduser()
        return p if p.is_absolute() else WORKSPACE_ROOT / p
    for c in (WORKSPACE_ROOT / "depot_tools", Path.home() / "depot_tools"):
        if c.is_dir():
            return c
    return WORKSPACE_ROOT / "depot_tools"


def setup_depot_tools(cfg: dict):
    d = resolve_depot_tools_dir(cfg)
    url = cget(cfg, "depot_tools_src", default=URL_DEPOT_TOOLS)
    if (d / ".git").is_dir():
        log(f"更新 depot_tools: {d}")
        git(["pull", "--ff-only"], cwd=d, check=False)
    elif d.exists():
        err(f"{d} 已存在但不是 git 仓库，请手动处理")
    else:
        log(f"克隆 depot_tools: {url} -> {d}")
        d.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", url, str(d)])
    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    os.environ["DEPOT_TOOLS_UPDATE"] = "0"
    os.environ["DEPOT_TOOLS_METRICS"] = "0"
    if IS_WIN:
        os.environ["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"   # 用本机 VS 工具链
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        os.environ.pop(k, None)
    log(f"depot_tools 就绪: {d}")


def gclient_bin() -> str:
    """Windows 下 gclient 是 .bat，CreateProcess 只补 .exe，需自己找。"""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        base = Path(d) / "gclient"
        for cand in ([base.with_suffix(".bat"), base.with_suffix(".cmd"), base] if IS_WIN else [base]):
            if cand.exists():
                return str(cand)
    return shutil.which("gclient") or "gclient"


# ── 通用仓库拉取 ────────────────────────────────────────────────────────────
def refspec_candidates(ref: str):
    """tag / branch 谁先试：含 '/' 的（如 flex/win）多半是分支，纯版本号多半是 tag。"""
    if ref.startswith("refs/") or re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return [ref]
    return ([f"refs/heads/{ref}", f"refs/tags/{ref}"] if "/" in ref
            else [f"refs/tags/{ref}", f"refs/heads/{ref}"])


def resolve_refspec(name: str, dest: Path, ref: str) -> str:
    """先 ls-remote 探一次（便宜），再决定拉哪个 refspec —— 避免为试错打一堆 fatal。"""
    for spec in refspec_candidates(ref):
        if out(["git", "ls-remote", "--exit-code", "origin", spec], cwd=dest):
            return spec
    warn(f"{name}: 远端既无 refs/tags/{ref} 也无 refs/heads/{ref}")
    return ""


def fetch_ref(name: str, dest: Path, ref: str, shallow: bool):
    depth = ["--depth", "1"] if shallow else []
    if not ref:
        git(["fetch", "--tags", *depth], cwd=dest, check=False)
        return
    spec = resolve_refspec(name, dest, ref)
    if not spec:
        return
    for attempt in range(1, 4):
        if git(["fetch", *depth, "origin", f"+{spec}:{spec}"], cwd=dest, check=False):
            return
        warn(f"{name}: fetch {spec} 第 {attempt}/3 次失败，20 秒后重试（大仓经代理常被中途掐断）")
        time.sleep(20)
    warn(f"{name}: fetch {ref} 连续 3 次失败（保留现有检出）")


def checkout_ref(name: str, dest: Path, ref: str):
    """切到目标 ref；工作区脏时只警告不切（绝不丢改动）。"""
    if not ref:
        return
    if git(["status", "--porcelain"], cwd=dest, capture=True):
        warn(f"{name} 工作区有未提交改动，保持当前 HEAD（提交/暂存后再跑会自动切到 {ref}）")
        return
    if not git(["checkout", ref], cwd=dest, check=False):
        warn(f"{name} 切不到 {ref}（保留 HEAD={git(['rev-parse', '--short', 'HEAD'], cwd=dest, capture=True) or '?'}）")
        return
    if git(["show-ref", "--verify", "--quiet", f"refs/heads/{ref}"], cwd=dest, check=False):
        git(["pull", "--ff-only", "origin", ref], cwd=dest, check=False)
    log(f"{name} 已切到 {ref} ({git(['rev-parse', '--short', 'HEAD'], cwd=dest, capture=True)})")


def assert_checkout_ok(name: str, dest: Path, ref: str):
    """fail-closed: 拉完必须有 HEAD。留一个空树会让后面的 sync / 挂载全在空目录上跑。"""
    if DRY_RUN:
        return
    if not git(["rev-parse", "-q", "--verify", "HEAD"], cwd=dest, capture=True):
        err(f"{name} 拉取后仍无 HEAD（{dest}）—— fetch/checkout 没成功（ref={ref or '默认分支'}）。\n"
            f"  常见原因: 代理中途掐断大仓传输 / ref 不存在。修好后重跑本脚本即可（已存在的空仓会被复用）。")


def clear_stale_locks(name: str, dest: Path):
    """大仓 fetch 被 Ctrl-C / 代理掐断会在 .git 里留 lock，之后每次 fetch 都直接
    fatal: Unable to create '.../shallow.lock'（看着像网络问题，其实是锁没清）。
    只清明显陈旧的（>2 分钟没动过）锁，刚写的锁说明另有 git 在跑，不动它。"""
    gitdir = dest / ".git"
    if not gitdir.is_dir():
        return
    for n in ("shallow.lock", "index.lock", "HEAD.lock", "packed-refs.lock"):
        p = gitdir / n
        if not p.exists():
            continue
        age = time.time() - p.stat().st_mtime
        if age < 120:
            err(f"{p} 刚被修改（{int(age)} 秒前）—— 可能有另一个 git 正在跑，等它结束再重跑 {name}")
        warn(f"清掉上次中断残留的锁: {p}（{int(age // 60)} 分钟没动过）")
        p.unlink()


def clone_or_update(name: str, dest: Path, url: str, ref: str, shallow: bool):
    if (dest / ".git").exists():
        log(f"{name} 已存在，更新: {dest}")
        clear_stale_locks(name, dest)
        git(["remote", "set-url", "origin", url], cwd=dest, check=False)
        fetch_ref(name, dest, ref, shallow)
        checkout_ref(name, dest, ref)
        assert_checkout_ok(name, dest, ref)
        return
    if dest.exists():
        if any(dest.iterdir()):
            err(f"{dest} 已存在但不是 git 仓库，请手动处理后再跑")
        # 上次拉取失败留下的空目录，删掉重来（留着会让后续 gclient 误判）
        warn(f"{dest} 是空目录（上次拉取失败的残留），删除后重新克隆")
        dest.rmdir()
    log(f"克隆 {name}: {url} -> {dest} (ref={ref or '默认分支'}{', 浅克隆' if shallow else ''})")
    if ref:
        # init + fetch 指定 refspec：只拉目标版本，不拖全部分支/历史
        dest.mkdir(parents=True, exist_ok=True)
        git(["init"], cwd=dest)
        git(["remote", "add", "origin", url], cwd=dest)
        fetch_ref(name, dest, ref, shallow)
        checkout_ref(name, dest, ref)
        assert_checkout_ok(name, dest, ref)
    else:
        git(["clone", *(["--depth", "1"] if shallow else []), url, str(dest)])


# ── .gclient ────────────────────────────────────────────────────────────────
def render_gclient(url: str, ver: str) -> str:
    pinned = url if "@" in url else (f"{url}@refs/tags/{ver}" if ver else url)
    return (
        "solutions = [\n"
        "  {\n"
        f'    "name": "src",\n'
        f'    "url": "{pinned}",\n'
        f'    "deps_file": "DEPS",\n'
        '    "managed": False,\n'
        '    "custom_deps": {},\n'
        '    "custom_vars": {},\n'
        '    "safesync_url": "",\n'
        "  },\n"
        "]\n"
        "cache_dir = None\n"
    )


def merge_target_os(text: str, extras) -> str:
    """合并 target_os，保留已有项（Android 依赖不能把宿主依赖删掉）。"""
    m = re.search(r"(?m)^[ \t]*target_os[ \t]*=\s*\[(.*?)\]", text, re.S)
    cur = re.findall(r'"([^"]+)"', m.group(1)) if m else [HOST_OS]
    merged = list(cur) + [e for e in extras if e not in cur]
    line = "target_os = [ " + ", ".join(f'"{x}"' for x in merged) + " ]"
    return text[:m.start()] + line + text[m.end():] if m else text.rstrip() + "\n" + line + "\n"


def sync_gclient(cfg: dict, url: str, ver: str, want_android: bool, force: bool):
    text = GCLIENT_FILE.read_text(encoding="utf-8", errors="replace") if GCLIENT_FILE.exists() else None
    if text is None:
        log(f"生成 .gclient（pin 到 {ver or '默认分支'}）: {GCLIENT_FILE}")
        text = render_gclient(url, ver)
    elif ver and ver not in text:
        if force:
            warn(f"--force: 覆盖 {GCLIENT_FILE}（重新 pin 到 {ver}）")
            text = render_gclient(url, ver)
        else:
            warn(f".gclient 已存在但未 pin 到 {ver} —— 保留现有配置（覆盖重跑加 --force）")
    if want_android:
        new = merge_target_os(text, ["android"])
        if new != text:
            log(".gclient 已加入 target_os=android（保留宿主 OS，免得删掉它的依赖）")
            text = new
    if GCLIENT_FILE.exists() and GCLIENT_FILE.read_text(encoding="utf-8", errors="replace") == text:
        return
    if DRY_RUN:
        log(f"(dry-run) 写入 {GCLIENT_FILE}")
        return
    GCLIENT_FILE.write_text(text, encoding="utf-8")


def has_target_android() -> bool:
    if not GCLIENT_FILE.exists():
        return False
    m = re.search(r"target_os\s*=\s*\[(.*?)\]", GCLIENT_FILE.read_text(encoding="utf-8", errors="replace"), re.S)
    return bool(m and "android" in m.group(1))


# ── 同步状态（DEPS 指纹）────────────────────────────────────────────────────
def deps_hash() -> str:
    f = CHROMIUM_SRC / "DEPS"
    if not f.exists():
        return ""
    return hashlib.sha256(f.read_bytes()).hexdigest()


def load_state() -> dict:
    p = STATE_DIR / "sync-state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict):
    if DRY_RUN:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "sync-state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def toolchain_paths(android: bool):
    if android:
        return [CHROMIUM_SRC / "third_party/android_toolchain/ndk",
                CHROMIUM_SRC / "third_party/android_sdk",
                CHROMIUM_SRC / "third_party/jdk"]
    gn = {"win": "buildtools/win/gn.exe", "mac": "buildtools/mac/gn",
          "linux": "buildtools/linux64/gn"}[HOST_OS]
    ninja = "third_party/ninja/ninja.exe" if IS_WIN else "third_party/ninja/ninja"
    return [CHROMIUM_SRC / gn, CHROMIUM_SRC / ninja]


def sync_reason(variant: str, want_hooks: bool, force: bool) -> str:
    if force:
        return "--force"
    st = load_state().get(variant)
    if not st:
        return "首次同步"
    h = deps_hash()
    if h and st.get("deps_hash") != h:
        return "DEPS 已变更"
    if want_hooks and not st.get("hooks"):
        return "上次未跑 hooks"
    missing = [p for p in toolchain_paths(variant == "android") if not p.exists()]
    if missing:
        return "关键依赖缺失: " + ", ".join(str(p.relative_to(CHROMIUM_SRC)) for p in missing)
    return ""


# ── gclient sync / runhooks ─────────────────────────────────────────────────
def gclient_sync(android: bool, nohooks: bool, jobs: int, shallow: bool, force: bool):
    variant = "android" if android else HOST_OS
    reason = sync_reason(variant, not nohooks, force)
    if not reason:
        log(f"依赖已是最新（{variant}，DEPS 未变且工具链在位），跳过 gclient sync")
        return
    log(f"gclient sync（{variant}）—— 原因: {reason}")

    if android and HOST != "Linux":
        warn(f"Chromium 的 Android 交叉编译只在 Linux 宿主上支持（当前: {HOST}）—— 继续拉依赖，但编译须换 Linux")
    sync_gclient(CFG, CHROMIUM_URL, VER, android, force)

    cmd = [gclient_bin(), "sync"]
    cmd += ["--no-history", "--shallow"] if android or shallow else ["--with_branch_heads", "--with_tags"]
    if nohooks:
        cmd += ["--nohooks"]
    cmd += ["--jobs", str(jobs)]

    bad = WORKSPACE_ROOT / "_bad_scm"
    for attempt in range(1, 4):
        log(f"第 {attempt}/3 次同步（--jobs {jobs}）")
        if run(cmd, cwd=WORKSPACE_ROOT, check=False):
            break
        warn("同步失败，清理 _bad_scm 后 30 秒重试（多为 googlesource 连接被切断）")
        shutil.rmtree(bad, ignore_errors=True)
        if attempt == 3:
            err("gclient sync 连续 3 次失败 —— 检查代理与网络")
        time.sleep(30)

    state = load_state()
    state[variant] = {"deps_hash": deps_hash(), "hooks": not nohooks,
                      "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(state)

    missing = [p for p in toolchain_paths(android) if not p.exists()]
    for p in missing:
        warn(f"工具链缺失: {p}")
    if nohooks:
        log("已跳过 hooks —— 补工具链: python3 scripts/fetch.py hooks")
    elif not missing:
        log("工具链就位")


def gclient_runhooks():
    if not GCLIENT_FILE.exists():
        err(f"缺少 {GCLIENT_FILE} —— 先跑: python3 scripts/fetch.py chromium")
    log("gclient runhooks（下载 clang / node / rust 等工具链）")
    run([gclient_bin(), "runhooks"], cwd=WORKSPACE_ROOT)
    state = load_state()
    for v in state.values():
        v["hooks"] = True
    save_state(state)


# ── 系统级依赖 ──────────────────────────────────────────────────────────────
def _cleanup_askpass():
    global _ASKPASS
    if _ASKPASS and os.path.exists(_ASKPASS):
        os.unlink(_ASKPASS)
    _ASKPASS = ""


def setup_sudo():
    """install-build-deps.sh 内部多次起 sudo，用一次性 askpass 免逐次输密码。"""
    if HOST != "Linux":
        return []
    if os.geteuid() == 0:
        return []
    if not shutil.which("sudo"):
        err("需要 root 权限：请安装 sudo 或以 root 运行本脚本")
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    if run(["sudo", "-n", "true"], check=False):
        return ["sudo", "-n"]
    if YES or not sys.stdin.isatty():
        err("非交互终端且 sudo 需要密码 —— 任选其一后重跑:\n"
            "  (a) 先 sudo -v 再跑本脚本\n"
            "  (b) 配免密: echo '<user> ALL=(ALL:ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/<user>\n"
            "  (c) 以 root 运行")
    global _ASKPASS
    fd, _ASKPASS = tempfile.mkstemp(prefix=".fetch_askpass.")
    with os.fdopen(fd, "w") as f:
        f.write(f"#!/bin/sh\nprintf %s {shlex.quote(getpass.getpass('sudo 密码: '))}\n")
    os.chmod(_ASKPASS, stat.S_IRWXU)
    os.environ["SUDO_ASKPASS"] = _ASKPASS
    atexit.register(_cleanup_askpass)
    if not run(["sudo", "-A", "-v"], check=False):
        err("sudo 密码不正确")
    return ["sudo", "-A"]


def apt_suite_missing(suite: str) -> bool:
    return f"a={suite}," not in out(["apt-cache", "policy"])


def ensure_apt_suites(sudo):
    """坑: apt 源缺 updates/backports 时，install-build-deps.sh 会因 -dev 包
    版本严格依赖（运行时库已被安全更新升级、-dev 仍是旧版）集体冲突失败。"""
    codename = ""
    f = Path("/etc/os-release")
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("UBUNTU_CODENAME=", "VERSION_CODENAME=")):
                codename = line.split("=", 1)[1].strip('"')
                if line.startswith("UBUNTU_CODENAME="):
                    break
    if not codename:
        codename = out(["lsb_release", "-cs"])
    if not codename:
        warn("识别不出 codename，跳过补 apt 源")
        return
    suites = [f"{codename}-updates", f"{codename}-backports"]
    missing = [s for s in suites if apt_suite_missing(s)]
    if not missing:
        log(f"apt 套件完整（含 {' '.join(suites)}）")
        return

    src = next((p for p in (Path("/etc/apt/sources.list.d/ubuntu.sources"),
                            Path("/etc/apt/sources.list.d/debian.sources"),
                            Path("/etc/apt/sources.list")) if p.exists()), None)
    if src is None:
        warn("找不到 apt 源配置文件，跳过补源（失败多半出在这里）")
        return
    warn(f"apt 缺少套件: {' '.join(missing)} —— 正在补（原文件会备份为 *.bak.<时间戳>）")
    if sudo:
        run([*sudo, "cp", "-a", str(src), f"{src}.bak.{time.strftime('%Y%m%d%H%M%S')}"])
    text = src.read_text(encoding="utf-8", errors="replace")
    if src.suffix == ".sources":                      # deb822：只给非 security 块补
        blocks = []
        for blk in text.strip("\n").split("\n\n"):
            if "security." in next((l for l in blk.splitlines() if l.startswith(("URIs:", "URIs :"))), ""):
                blocks.append(blk)
                continue
            fixed = []
            for l in blk.splitlines():
                if l.startswith(("Suites:", "Suites :")):
                    cur = l.partition(":")[2].split()
                    add = [s for s in missing if s not in cur]
                    if add:
                        l = l.rstrip() + " " + " ".join(add)
                fixed.append(l)
            blocks.append("\n".join(fixed))
        text = "\n\n".join(blocks) + "\n"
    else:
        text = text.rstrip() + "\n" + "".join(
            f"\n# added by fetch.py\ndeb http://archive.ubuntu.com/ubuntu {s} main restricted universe multiverse\n"
            for s in missing if not s.endswith("-security"))
    tmp = src.with_suffix(src.suffix + ".fetch.tmp")
    tmp.write_text(text, encoding="utf-8")
    if sudo:
        run([*sudo, "cp", str(tmp), str(src)])
        run([*sudo, "apt-get", "update"], check=False)
    else:
        src.write_text(text, encoding="utf-8")
        run(["apt-get", "update"], check=False)
    tmp.unlink(missing_ok=True)
    log(f"apt 源已更新: {src}")


def do_sysdeps(android: bool):
    step("系统级依赖")
    if HOST == "Linux":
        script = CHROMIUM_SRC / "build/install-build-deps.sh"
        if not script.exists():
            err(f"缺少 {script} —— 先跑: python3 scripts/fetch.py chromium")
        sudo = setup_sudo()
        if not android:
            ensure_apt_suites(sudo)
        log("运行 install-build-deps.sh（十几到几十分钟，视网速）")
        cmd = [*sudo, "env", "DEBIAN_FRONTEND=noninteractive", "bash", str(script), "--no-prompt"]
        if android:
            cmd.append("--android")
        t0 = time.time()
        if not run(cmd, check=False):
            err("install-build-deps.sh 失败 —— 看上方错误；常见原因: apt 源缺 updates（已自动补）/ 磁盘不足")
        log(f"系统依赖就绪，耗时 {int(time.time() - t0) // 60} 分钟")
    elif HOST == "Darwin":
        dev = out(["xcode-select", "-p"])
        if not dev or "Xcode" not in dev:
            warn("xcode-select 未指向完整 Xcode: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer")
        else:
            log(f"Xcode: {dev}")
        if not out(["xcrun", "clang", "--version"]):
            warn("xcrun clang 不可用 —— 可能未接受许可: sudo xcodebuild -license accept")
    else:
        log("Windows: 系统依赖不自动安装，请自备 Visual Studio 2022+ 与 Windows 11 SDK")
        vs = next((p for p in Path(r"C:\Program Files\Microsoft Visual Studio").glob("2022/*")
                   if (p / "Common7/Tools/VsDevCmd.bat").exists()), None) if Path(r"C:\Program Files\Microsoft Visual Studio").exists() else None
        if vs:
            log(f"Visual Studio: {vs}")
        else:
            warn("未找到 Visual Studio 2022+，编译大概率失败")


# ── 各目标 ──────────────────────────────────────────────────────────────────
def do_chromium(shallow: bool):
    if not VER:
        warn("未指定版本（--ver / .env chromium_ver）—— 拉默认分支")
    clone_or_update("chromium/src", CHROMIUM_SRC, CHROMIUM_URL, VER, shallow)
    sync_gclient(CFG, CHROMIUM_URL, VER, False, FORCE)


def do_kernel(cfg: dict):
    url = cget(cfg, "nomad_kernel_src")
    if not url:
        err("未配置 nomad_kernel_src（.env）")
    # nomad 仓要能切分支/回看历史，不浅克隆
    clone_or_update("nomadbrowser.kernel", KERNEL_DIR, url, cget(cfg, "kernel_ref", "nomad_kernel_ver"), False)


def do_pc(cfg: dict):
    url = cget(cfg, "nomad_pc_src")
    if not url:
        err("未配置 nomad_pc_src（.env）")
    clone_or_update("nomadbrowser.pc", PC_DIR, url, cget(cfg, "pc_ref", "nomad_pc_ver"), False)


def do_link():
    """编进产物的是挂载点那份代码：挂载错 = 编错树，故拉取收尾必做。"""
    link = CHROMIUM_SRC / MODULE_RELDIR
    if not CHROMIUM_SRC.is_dir():
        err(f"src/ 不存在，无法挂载: {CHROMIUM_SRC}")
    if not KERNEL_DIR.is_dir():
        err(f"内核仓不存在，无法挂载: {KERNEL_DIR}")

    if link.is_symlink():
        if os.readlink(link) == str(KERNEL_DIR):
            log(f"挂载已就位: {MODULE_RELDIR} -> {KERNEL_DIR}")
            return
        warn(f"挂载点当前指向 {os.readlink(link)}，改为 {KERNEL_DIR}")
        link.unlink()
    elif link.exists():
        if (link / "BUILD.gn").exists():
            log(f"挂载已就位（junction/拷贝）: {link}")
            return
        warn(f"{link} 已存在且无法识别为挂载（无 BUILD.gn），保留不动")
        return

    link.parent.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        # junction 不需要管理员权限；失败则退回拷贝
        if not run(["cmd", "/c", "mklink", "/J", str(link), str(KERNEL_DIR)], check=False):
            warn("创建 junction 失败，退回拷贝挂载")
            shutil.copytree(KERNEL_DIR, link, dirs_exist_ok=True)
    else:
        link.symlink_to(KERNEL_DIR, target_is_directory=True)
    log(f"已挂载: {MODULE_RELDIR} -> {KERNEL_DIR}")


# ── CLI ─────────────────────────────────────────────────────────────────────
TARGETS = ("depot_tools", "chromium", "v8", "deps", "hooks", "android", "sysdeps",
           "kernel", "pc", "link", "all")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fetch.py",
        description="工作区源码 / 依赖拉取更新（Windows / macOS / Linux 统一入口）",
        epilog="示例:\n"
               "  python3 scripts/fetch.py                 拉全部源码 + 挂载\n"
               "  python3 scripts/fetch.py deps            gclient sync + hooks\n"
               "  python3 scripts/fetch.py deps --nohooks --jobs 8   只同步依赖\n"
               "  python3 scripts/fetch.py hooks           gclient runhooks\n"
               "  python3 scripts/fetch.py android         Android 依赖（宿主须 Linux）\n"
               "  python3 scripts/fetch.py sysdeps         Linux: install-build-deps.sh\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("targets", nargs="*", metavar="目标",
                   help=" / ".join(TARGETS) + "（默认 all）")
    p.add_argument("--ver", dest="ver", default="", help="chromium 版本标签")
    p.add_argument("--proxy", default="", help="HTTP 代理 URL")
    p.add_argument("--jobs", type=int, default=8, help="gclient 并行度（默认 8）")
    p.add_argument("--nohooks", action="store_true", help="deps 只同步依赖，不跑 hooks")
    p.add_argument("--shallow", dest="shallow", action="store_const", const=True, default=None,
                   help="浅克隆 / gclient --no-history（chromium 默认开）")
    p.add_argument("--full-history", dest="shallow", action="store_const", const=False, default=None,
                   help="拉全历史（关掉浅克隆）")
    p.add_argument("--force", action="store_true", help="强制 sync / 覆盖 .gclient")
    p.add_argument("--sysdeps", action="store_true", help="顺带装系统级依赖（Linux 首次建议）")
    p.add_argument("--no-link", action="store_true", help="all 时跳过内核挂载")
    p.add_argument("-y", "--yes", action="store_true", help="非交互")
    p.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    # clone.sh 旧选项别名
    p.add_argument("--sync", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-hooks", dest="nohooks", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-history", dest="shallow", action="store_const", const=True, default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--force-gclient", dest="force", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    global DRY_RUN, YES, CFG, VER, CHROMIUM_URL, FORCE
    args = build_parser().parse_args(argv)

    DRY_RUN = args.dry_run
    YES = args.yes
    CFG = load_config()
    VER = args.ver or cget(CFG, "chromium_ver", "CHROMIUM_VERSION")
    FORCE = args.force

    targets = [t for t in args.targets if t in TARGETS]
    unknown = [t for t in args.targets if t not in TARGETS]
    if unknown:
        err("未知目标: " + " ".join(unknown) + "（-h 查看用法）")
    if args.sync:
        targets.append("deps")
    if not targets:
        targets = ["all"]

    proxy = apply_proxy(CFG, args.proxy)
    CHROMIUM_URL = chromium_url(CFG, proxy)
    shallow = True if args.shallow is None else args.shallow
    if cget(CFG, "clone_history") == "shallow":
        shallow = True

    log(f"工作区: {WORKSPACE_ROOT}")
    log(f"宿主: {HOST} ({HOST_OS})   版本: {VER or '未指定'}   浅克隆: {'是' if shallow else '否'}")
    if not shutil.which("git"):
        err("未找到 git —— 请先安装（macOS: xcode-select --install；Windows: Git for Windows）")

    # all 展开成具体目标，保留用户额外指定的目标（去重、保序）
    expanded: list = []
    for t in targets:
        if t == "all":
            expanded += ["depot_tools", "chromium", "v8", "kernel", "pc"]
            if not args.no_link:
                expanded.append("link")
        else:
            expanded.append(t)
    if args.sysdeps and "sysdeps" not in expanded:
        expanded.insert(0, "sysdeps")

    seen = set()
    targets = [t for t in expanded if not (t in seen or seen.add(t))]

    done = []
    for t in targets:
        step(t)
        if t == "depot_tools":
            setup_depot_tools(CFG)
        elif t == "chromium":
            do_chromium(shallow)
        elif t == "deps":
            gclient_sync(False, args.nohooks, args.jobs, args.shallow is True, args.force)
        elif t == "hooks":
            gclient_runhooks()
        elif t == "android":
            if args.sysdeps:
                do_sysdeps(True)
            gclient_sync(True, args.nohooks, args.jobs, True, args.force)
        elif t == "sysdeps":
            do_sysdeps(False)
        elif t == "kernel":
            do_kernel(CFG)
        elif t == "pc":
            do_pc(CFG)
        elif t == "link":
            do_link()
        done.append(t)

    step("完成")
    log("已执行: " + " ".join(done))
    if "deps" not in done and "android" not in done and "hooks" not in done:
        print("""
下一步:
  依赖同步 : python3 scripts/fetch.py deps
  Android  : python3 scripts/fetch.py android""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("已中断")
