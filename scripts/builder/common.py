#!/usr/bin/env python3
# =============================================================================
#  构建编排公共层 —— 平台/架构识别、目录命名、命令执行、交付计数
#
#  命名规范（工作区级约定，三个项目统一）:
#    编译目录  out/<project>-<os>-<arch>-<ver>-<static|dynamic>
#    交付目录  dist/<project>-<os>-<arch>-<ver>-<n>          （n = 第几次交付，自动递增）
#
#  与 fetch.py 的分工: fetch 只管"把源码/依赖弄齐"，本层只管"把它编出来并出包"；
#  两边共用同一份 .env 与 depot_tools 解析。
# =============================================================================
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]      # scripts/builder/ -> 工作区根
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
SRC = WORKSPACE_ROOT / "src"
KERNEL_REPO = WORKSPACE_ROOT / "nomadbrowser.kernel"
PC_REPO = WORKSPACE_ROOT / "nomadbrowser.pc"
ANDROID_REPO = WORKSPACE_ROOT / "nomadbrowser.android"
# 坑: 编译目录在 src/ 之外时，Windows 的 midl ACTION 会集体失败（midl.exe 把 GN rebase
# 出来的 ../../src/... 写进注释，与按 src/out/<name> 布局生成的 checked-in 基线比对不上）。
# 由 ensure_midl_out_of_tree_patch() 打补丁解决，目录位置保持工作区根的 out/。
OUT_ROOT = WORKSPACE_ROOT / "out"
DIST_ROOT = WORKSPACE_ROOT / "dist"
STATE_DIR = WORKSPACE_ROOT / ".build"
TOOLS_DIR = SCRIPTS_DIR / "tools"

HOST = platform.system()
HOST_OS = {"Windows": "win", "Darwin": "mac", "Linux": "linux"}.get(HOST, "linux")
IS_WIN = HOST == "Windows"

DRY_RUN = False
YES = False


# ── 输出 ────────────────────────────────────────────────────────────────────
def _c(code: str, s: str) -> str:
    use = not IS_WIN and sys.stdout.isatty()
    return f"\033[{code}m{s}\033[0m" if use else s


def log(msg):  print(_c("1;34", "[INFO] ") + str(msg), flush=True)
def warn(msg): print(_c("1;33", "[WARN] ") + str(msg), file=sys.stderr, flush=True)
def step(msg): print("\n" + _c("1;36", f"===== {msg} ====="), flush=True)


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
    cmd = [str(c) for c in cmd]
    if DRY_RUN:
        log("(dry-run) " + " ".join(shlex.quote(c) for c in cmd))
        return True
    log("执行: " + " ".join(shlex.quote(c) for c in cmd))
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


# ── 配置（复用 fetch.py 的 .env 解析，避免两份实现漂移）──────────────────────
sys.path.insert(0, str(SCRIPTS_DIR))
import fetch  # noqa: E402  （scripts/fetch.py：.env / 代理 / depot_tools 解析）

load_config = fetch.load_config
cget = fetch.cget
apply_proxy = fetch.apply_proxy
resolve_depot_tools_dir = fetch.resolve_depot_tools_dir


def apply_developer_dir(cfg: dict, *keys):
    """macOS 上一台机器常并存多个 Xcode，而不同项目要求的 Swift/SDK 版本并不一样：
    Chromium 树随版本绑定某个 SDK（换一个数小时全量重编），PC 的 macOS 原生 helper
    却只能用 Swift 版本对得上的那份，否则
        this SDK is not supported by the compiler ... Please select a toolchain
        which matches the SDK.
    按项目各取所需，互不干扰。
    优先级: 外部已 export 的 DEVELOPER_DIR > .env <keys> > 不动（沿用 xcode-select）。"""
    if HOST_OS != "mac" or os.environ.get("DEVELOPER_DIR"):
        return
    d = cget(cfg, *keys)
    if not d:
        return
    if not Path(d).is_dir():
        warn(f"配置的 DEVELOPER_DIR 不存在，忽略: {d}")
        return
    os.environ["DEVELOPER_DIR"] = d
    log(f"DEVELOPER_DIR（{keys[0]}）: {d}")


def ensure_depot_tools(cfg: dict):
    """depot_tools 进 PATH（不更新它 —— 构建阶段不该顺手拉仓库）。"""
    d = resolve_depot_tools_dir(cfg)
    if not d.is_dir():
        err(f"depot_tools 不存在: {d}（先跑: python3 scripts/fetch.py depot_tools）")
    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    # depot_tools 的 ninja.py 用 gclient_paths.FindGclientRoot(out_dir) 反推源码根：
    # out/ 不在 src 下时它返回 None，autoninja 会报 "Could not find Ninja in the
    # third_party..."。把树内 ninja 挂进 PATH，fallback 就能找到它（扫描时跳过 depot_tools）。
    ninja_dir = ninja_path().parent
    if ninja_dir.is_dir():
        os.environ["PATH"] = str(ninja_dir) + os.pathsep + os.environ.get("PATH", "")
    os.environ["DEPOT_TOOLS_UPDATE"] = "0"
    os.environ["DEPOT_TOOLS_METRICS"] = "0"
    if IS_WIN:
        os.environ["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        os.environ.pop(k, None)
    log(f"depot_tools: {d}")
    fetch.bootstrap_depot_tools(d)   # 缺自举产物时 autoninja 会 exit 1（幂等，只下一回）
    ensure_midl_out_of_tree_patch()  # Windows + 编译目录在 src 外: midl 基线比对补丁
    return d


MIDL_PY = SRC / "build" / "toolchain" / "win" / "midl.py"
MIDL_MARKER = "nomad: normalize_idl_path_comment"
MIDL_HELPER = (
    "# " + MIDL_MARKER + "\n"
    "def normalize_idl_path_comment(filename):\n"
    '    """抹掉 midl 注释里多出来的 src/ 层级（只改参与比对的两份临时副本）。"""\n'
    "    contents = open(filename, 'rb').read()\n"
    "    contents = re.sub(rb'(Compiler settings for (?:\\.\\./)+)src/', rb'\\1', contents)\n"
    "    open(filename, 'wb').write(contents)\n"
    "\n\n"
)


def ensure_midl_out_of_tree_patch() -> bool:
    """编译目录不在 src/ 下时，让 Windows 的 midl ACTION 仍能通过基线比对。

    midl.exe 把传给它的 idl 路径原样写进生成文件注释:
        /* Compiler settings for ../../src/third_party/.../X.idl:     ← out/ 在工作区根
    而 src/third_party/win_build_output/midl/ 下的 checked-in 基线是按上游布局（编译目录
    src/out/<name>）生成的 ../../third_party/...，midl.py 拿 filecmp 逐字节比对 →
    全线 FAILED: "midl.exe output different from files in ..."（差异只有这行注释）。

    把参与比对的两侧（midl 输出 + 从基线拷到 out 的那份副本）各规范化一次，抹掉多出来的
    src/ 层级即可对齐：仓库里的基线原件一个字节都不动（不像 rebaseline 那样污染 src），
    产物内容也只差注释。幂等：按标记判断是否已打过，切版本/重拉源码后重跑会自动补上。
    """
    if not IS_WIN:
        return False
    if not MIDL_PY.exists():
        return False
    text = MIDL_PY.read_text(encoding="utf-8", errors="replace")
    if MIDL_MARKER in text:
        return False
    if DRY_RUN:                       # 改源码树的操作，dry-run 下只看不做
        log(f"(dry-run) 打 midl 基线补丁: {MIDL_PY}")
        return False
    edits = [
        # 1) midl 输出（ZapTimestamp 之后）
        ("            ZapTimestamp(os.path.join(midl_output_dir, f))\n",
         "            ZapTimestamp(os.path.join(midl_output_dir, f))\n"
         "            normalize_idl_path_comment(os.path.join(midl_output_dir, f))\n"),
        # 2) 从 checked-in 基线拷进 outdir 的那份副本
        ("        shutil.copy(file_path, outdir)\n",
         "        shutil.copy(file_path, outdir)\n"
         "        normalize_idl_path_comment(os.path.join(outdir, source_file))\n"),
        # 3) 辅助函数本体
        ("def main(\n    arch,", MIDL_HELPER + "def main(\n    arch,"),
    ]
    for old, new in edits:
        if text.count(old) != 1:
            warn(f"{MIDL_PY.name} 结构变了（锚点不唯一: {old.strip()[:48]}）—— 跳过补丁；"
                 f"若 midl ACTION 仍报输出与基线不一致，需手动处理 {MIDL_PY}")
            return False
        text = text.replace(old, new)
    MIDL_PY.write_text(text, encoding="utf-8")
    log(f"已给 {MIDL_PY.relative_to(SRC)} 打补丁: 比对前规范化 idl 路径注释"
        f"（编译目录在 src 外时的 midl 基线差异）")
    return True


def android_native_deps():
    """编 Chromium Android 必需的三件（DEPS 里带，靠 .gclient 的 target_os=android 拉下来）。"""
    return [SRC / "third_party/android_toolchain/ndk",
            SRC / "third_party/android_sdk",
            SRC / "third_party/jdk"]


def ensure_android_native_deps():
    """缺任一件都别开编 —— 缺 NDK/SDK 时 gn/ninja 甩的是几十屏噪音，不如先说清。"""
    miss = [p for p in android_native_deps() if not p.exists()]
    if miss:
        err("Android 依赖缺失:\n  " + "\n  ".join(str(p.relative_to(SRC)) for p in miss) +
            "\n  修: python3 scripts/fetch.py android")


def gclient_bin() -> str:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        base = Path(d) / "gclient"
        for cand in ([base.with_suffix(".bat"), base.with_suffix(".cmd"), base] if IS_WIN else [base]):
            if cand.exists():
                return str(cand)
    return shutil.which("gclient") or "gclient"


# ── 平台 / 架构 ─────────────────────────────────────────────────────────────
PROJECTS = ("chromium", "kernel", "browser", "android")
PROJECT_ALIAS = {"baseline": "chromium", "chrome": "chromium",
                 "pc": "browser", "nomad": "browser", "browser": "browser",
                 "arupa": "kernel", "nomad_kernel": "kernel",
                 "apk": "android", "nomad_android": "android", "android": "android"}
OSES = ("win", "mac", "linux", "android")
ARCHS = ("x86", "x64", "arm64")
LINK_MODES = ("static", "dynamic")

ARCH_ALIAS = {"apple": "arm64", "aarch64": "arm64", "arm": "arm64",
              "x86_64": "x64", "amd64": "x64", "i386": "x86", "i686": "x86"}

# os -> 该平台允许的架构
OS_ARCHS = {
    "win": ("x86", "x64", "arm64"),
    "mac": ("x64", "arm64"),          # macOS 已不支持 x86（32 位）
    "linux": ("x64", "arm64"),
    "android": ("x86", "x64", "arm64"),
}


def normalize_project(p: str) -> str:
    p = p.lower()
    return PROJECT_ALIAS.get(p, p)


def normalize_arch(a: str) -> str:
    a = a.lower()
    return ARCH_ALIAS.get(a, a)


def normalize_link(l: str) -> str:
    l = (l or "").lower()
    return {"release": "static", "dev": "dynamic"}.get(l, l)


def host_arch() -> str:
    m = platform.machine().lower()
    return ARCH_ALIAS.get(m, m)


def check_project(project: str, os_name: str, arch: str):
    """交叉编译的现实约束：Chromium 只能在本宿主编本宿主（macOS 目标必须 macOS 宿主，
    Windows 目标必须 Windows 宿主），Android 目标需要 Linux 宿主。"""
    if project == "android" and os_name != "android":
        err(f"android 项目只出 Android 包（当前目标系统: {os_name}）—— 加 --os android")
    if os_name not in OSES:
        err(f"不支持的系统: {os_name}（可选: {' / '.join(OSES)}）")
    if arch not in ARCHS:
        err(f"不支持的架构: {arch}（可选: {' / '.join(ARCHS)}）")
    if arch not in OS_ARCHS[os_name]:
        err(f"{os_name} 不支持 {arch} 架构（可选: {' / '.join(OS_ARCHS[os_name])}）")
    if os_name == "android":
        if HOST_OS != "linux":
            warn(f"Android 交叉编译只在 Linux 宿主上成立（当前: {HOST_OS}）—— 继续但大概率失败")
        return
    if os_name != HOST_OS:
        err(f"{os_name} 目标必须在 {os_name} 宿主上编译（当前: {HOST_OS}）—— "
            f"Chromium 不支持这种交叉编译（换 gn 路径也没用）")


# ── 目录命名 ────────────────────────────────────────────────────────────────
def build_dir_name(project: str, os_name: str, arch: str, ver: str, link: str) -> str:
    """out/<project>-<os>-<arch>-<ver>-<static|dynamic>"""
    return f"{project}-{os_name}-{arch}-{ver}-{link}"


def dist_prefix(project: str, os_name: str, arch: str, ver: str) -> str:
    """dist/<project>-<os>-<arch>-<ver>-<n> 的前缀（不含 n）"""
    return f"{project}-{os_name}-{arch}-{ver}"


@dataclass
class Ctx:
    project: str
    os: str
    arch: str
    ver: str
    link: str
    variant: str = "debug"          # android 项目: debug / release（Gradle variant）
    jobs: int = 0
    cfg: dict = field(default_factory=dict)
    no_link: bool = False
    keep_history: bool = False
    delivery_no: int = 0
    gate: bool = True
    webui: bool = True
    delivery_root: str = ""

    @property
    def out_name(self) -> str:
        return build_dir_name(self.project, self.os, self.arch, self.ver, self.link)

    @property
    def out_dir(self) -> Path:
        return OUT_ROOT / self.out_name

    @property
    def dist_pre(self) -> str:
        return dist_prefix(self.project, self.os, self.arch, self.ver)

    def dist_name(self, n: int) -> str:
        return f"{self.dist_pre}-{n}"


# ── 版本 / 交付计数 ─────────────────────────────────────────────────────────
def chromium_version(cfg: dict) -> str:
    """取自源码树 chrome/VERSION（避免手抄漂移）；回退 .env chromium_ver。"""
    f = SRC / "chrome" / "VERSION"
    if f.exists():
        kv = {}
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        if all(k in kv for k in ("MAJOR", "MINOR", "BUILD", "PATCH")):
            return ".".join(kv[k] for k in ("MAJOR", "MINOR", "BUILD", "PATCH"))
    v = cget(cfg, "chromium_ver", "CHROMIUM_VERSION")
    if not v:
        err(f"取不到版本号：{f} 不存在且 .env 无 chromium_ver")
    warn(f"用 .env chromium_ver 兜底: {v}（源码树 {f} 缺失）")
    return v


def next_delivery_no(c: Ctx) -> int:
    """扫描 dist/ 里同前缀已有编号，取 max+1；状态文件只做留痕。"""
    if c.delivery_no:
        return c.delivery_no
    pat = re.compile(r"^" + re.escape(c.dist_pre) + r"-(\d+)$")
    mx = 0
    if DIST_ROOT.is_dir():
        for p in DIST_ROOT.iterdir():
            m = pat.match(p.name) or pat.match(p.name[:-4] if p.name.endswith(".zip") else "")
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1


def record_delivery(c: Ctx, n: int, root: Path, artifacts: list):
    if DRY_RUN:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    st_path = STATE_DIR / "delivery-state.json"
    st = json.loads(st_path.read_text(encoding="utf-8")) if st_path.exists() else {}
    st[c.dist_name(n)] = {
        "project": c.project, "os": c.os, "arch": c.arch, "ver": c.ver,
        "link": c.link, "root": str(root), "artifacts": artifacts,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kernel_head": out(["git", "rev-parse", "--short", "HEAD"], cwd=KERNEL_REPO) or "?",
    }
    st_path.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (DIST_ROOT / f"{c.dist_name(n)}.gate.json").write_text(
        json.dumps(st[c.dist_name(n)], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def purge_previous_deliveries(c: Ctx, keep: str):
    """dist/ 只留本次（同 project-os-arch-ver 的旧编号全清）。"""
    if c.keep_history:
        log("KEEP_HISTORY=1 —— 保留历史交付包")
        return
    pat = re.compile(r"^" + re.escape(c.dist_pre) + r"-\d+(\.zip|\.gate\.json)?$")
    n = 0
    if DIST_ROOT.is_dir():
        for p in DIST_ROOT.iterdir():
            if not pat.match(p.name) or p.name.startswith(keep):
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            n += 1
    if n:
        log(f"已清理 {n} 个同前缀旧交付（dist/ 只留本次；保留历史用 --keep-history）")


# ── 工具链路径 ──────────────────────────────────────────────────────────────
def gn_path(os_name: str) -> Path:
    # gn 是主机工具：android 是交叉编译目标，没有 buildtools/android/gn，按宿主机取
    key = os_name if os_name in ("win", "mac", "linux") else HOST_OS
    rel = {"win": "buildtools/win/gn.exe", "mac": "buildtools/mac/gn",
           "linux": "buildtools/linux64/gn"}.get(key, f"buildtools/{key}/gn")
    return SRC / rel


def ninja_path() -> Path:
    return SRC / ("third_party/ninja/ninja.exe" if IS_WIN else "third_party/ninja/ninja")


def autoninja_cmd(c: Ctx) -> list:
    """autoninja 启动前缀（depot_tools 包装: 按 args.gn 的 use_siso 派发 ninja/siso，并自动选 -j）。

    Windows 上两个连环坑，踩中的表现都是"命令无法执行 / exit 1":
      1. autoninja 落地是 autoninja.bat —— subprocess 走 CreateProcess，不补 .bat 扩展名
         （gclient 同理，见 fetch.gclient_bin），直接 [WinError 2] 系统找不到指定的文件。
      2. autoninja.bat 内部是 python-bin\\python3.bat autoninja.py，而 python3.bat 硬性要求
         python3_bin_reldir.txt（depot_tools 自举产物，未自举时没有）—— 光解决 1 会得到
         exit 1: python3_bin_reldir.txt not found。gclient.bat 用的是 vpython3（自带
         hermetic CPython，不依赖自举），所以未自举时走 vpython3 + autoninja.py，
         与 autoninja.bat 完全等价（它跑的就是 autoninja.py）。
    """
    if not IS_WIN:
        return ["autoninja"]
    d = resolve_depot_tools_dir(c.cfg)
    if (d / "python3_bin_reldir.txt").exists():
        for n in ("autoninja.bat", "autoninja.cmd"):
            if (d / n).exists():
                return [str(d / n)]
    vpy = next((d / n for n in ("vpython3.bat", "vpython3.cmd", "vpython3.exe")
                if (d / n).exists()), None)
    if vpy and (d / "autoninja.py").exists():
        log("depot_tools 未自举（缺 python3_bin_reldir.txt）—— 改用 vpython3 跑 autoninja.py")
        return [str(vpy), str(d / "autoninja.py")]
    return ["autoninja"]


def build_cmd(c: Ctx, targets) -> list:
    """autoninja（depot_tools 包装，能按 args.gn 的 use_siso 派发后端）。"""
    cmd = [*autoninja_cmd(c), "-C", str(c.out_dir)]
    if c.jobs:
        cmd += ["-j", str(c.jobs)]
    cmd += list(targets)
    return cmd


def probe_steps(c: Ctx, targets, timeout: int = 90):
    """ninja -n 探一次规模：明确 "no work to do" 才跳；探不清/超时一律照编
    （ninja 自己是增量引擎，真没工作时它零点几秒就返回）。返回 None = 未知。"""
    if os.environ.get("NO_PROBE") or DRY_RUN:
        return None
    ninja = ninja_path()
    if not ninja.exists() or not (c.out_dir / "build.ninja").exists():
        return None
    try:
        r = subprocess.run([str(ninja), "-C", str(c.out_dir), "-n", *targets], cwd=SRC,
                           text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        warn(f"规模探测超时（{timeout}s，大仓冷启动慢）—— 照编")
        return None
    txt = r.stdout
    if "no work to do" in txt:
        return 0
    m = re.findall(r"\[(\d+)/(\d+)\]", txt)
    return int(m[-1][1]) if m else None


def gn_gen(c: Ctx, args_file: Path):
    gn = gn_path(c.os)
    if not gn.exists():
        err(f"gn 不存在: {gn}（先跑: python3 scripts/fetch.py deps —— 它会拉 buildtools）")
    run([str(gn), "gen", str(c.out_dir)], cwd=SRC)


# ── args.gn ─────────────────────────────────────────────────────────────────
BAD_GN_ARGS = ("mac_sdk_path",)   # gn 会直接拒绝的残留参数（见 README 常见坑 #2）


def purge_bad_gn_args(args_file: Path):
    """历史输出目录里残留的非法 arg 会让每次 gn gen 都在同一处炸 —— 注释掉（幂等）。"""
    if not args_file.exists():
        return
    for bad in BAD_GN_ARGS:
        text = args_file.read_text(encoding="utf-8", errors="replace")
        if not re.search(rf"^[ \t]*{bad}[ \t]*=", text, re.M):
            continue
        fixed = re.sub(rf"^([ \t]*)({bad}[ \t]*=)", rf"# gn 拒绝该 arg: \2", text, flags=re.M)
        if DRY_RUN:
            log(f"(dry-run) 注释掉 {bad}: {args_file}")
            continue
        args_file.write_text(fixed, encoding="utf-8")
        warn(f"args.gn 残留 {bad}（gn 会拒绝），已注释掉: {args_file}")


def render_args_gn(header: str, args) -> str:
    lines = [header]
    for k, v in args:
        lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


def write_args_gn(args_file: Path, header: str, args, extra: str = "") -> bool:
    """幂等：与现有内容逐行一致就复用（不冲掉手改，也不白触发 regen）。"""
    args_file.parent.mkdir(parents=True, exist_ok=True)
    purge_bad_gn_args(args_file)
    want = {k: v for k, v in args}
    if args_file.exists():
        cur = args_file.read_text(encoding="utf-8", errors="replace")
        have = dict(re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", cur))
        if all(have.get(k) == v for k, v in want.items()):
            log(f"args.gn 已与当前配置一致，复用: {args_file}")
            return False
        warn(f"args.gn 与当前配置不符，重写（会触发 gn regen）: {args_file}")
    text = render_args_gn(header, args)
    if extra:
        text += "\n" + extra.strip() + "\n"
    if DRY_RUN:
        log(f"(dry-run) 写入 {args_file}")
        return True
    args_file.write_text(text, encoding="utf-8")   # 无 BOM：gn 不认 BOM
    log(f"已写入 args.gn: {args_file}")
    return True


def extra_gn_args(c: Ctx) -> str:
    """.env 里的 gn_args_<project>_<os> 可追加平台专属参数（如 sysroot / android 开关）。"""
    return cget(c.cfg, f"gn_args_{c.project}_{c.os}", f"gn_args_{c.project}", default="")


# ── 产物 / 归档 ─────────────────────────────────────────────────────────────
def human_size(p: Path) -> str:
    if p.is_dir():
        return out(["du", "-sh", str(p)]).split()[0] if not IS_WIN else "?"
    n = p.stat().st_size
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(root: Path):
    rows = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            rows.append(f"{sha256_file(p)}  {p.relative_to(root).as_posix()}")
    (root / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    log(f"已生成 SHA256SUMS.txt（{len(rows)} 件）")


def zip_dir(src: Path, zip_path: Path, mac_keep_symlink: bool = False):
    """mac 交付包里的 macKernel -> kernel 软链要存成链接（zip -y），否则 dylib 存两份。"""
    if zip_path.exists():
        zip_path.unlink()
    if shutil.which("zip"):
        cmd = ["zip", "-q", "-r"] + (["-y"] if mac_keep_symlink else []) + \
              [str(zip_path), "."]
        if run(cmd, cwd=src, check=False):
            return zip_path
    if DRY_RUN:
        return zip_path
    base = shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=src)
    Path(base).rename(zip_path)
    return zip_path
