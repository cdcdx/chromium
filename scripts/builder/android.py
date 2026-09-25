#!/usr/bin/env python3
# =============================================================================
#  nomad Android 浏览器（APK）编译 / 打包
#
#  分工: kernel 编 native（并出 arupa-kernel.aar），本层只管把 nomadbrowser.android
#  用 Gradle 编成 APK。两边靠 app/libs/kernel/<abi>/arupa-kernel.aar 衔接 —— AAR 的
#  sha256 写在 tools/ci/runtime-manifest.json，Gradle 在**配置期**就校验，版本/ABI
#  对不上会在起步阶段炸，所以这里提前把话说清，省得翻几十屏堆栈。
#
#  Gradle 产物沿用它自己的 app/build/outputs/apk/<variant>/
#  交付目录: dist/android-<os>-<arch>-<ver>-<n>
# =============================================================================
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from .common import (ANDROID_REPO, DIST_ROOT, Ctx, err, log, out, run, warn,
                     next_delivery_no, record_delivery, human_size,
                     purge_previous_deliveries, write_sha256sums)
from . import common

GRADLEW = ANDROID_REPO / "gradlew"
RUNTIME_MANIFEST = ANDROID_REPO / "tools" / "ci" / "runtime-manifest.json"
ACTIONS = ("down", "build", "package", "manifest")

# 本层 arch -> Gradle 的 -PkernelAbi（app/build.gradle 只认 arm64 / x64）
KERNEL_ABI = {"arm64": "arm64", "x64": "x64", "x86": "x64"}


def _require_repo():
    if not ANDROID_REPO.is_dir():
        err(f"Android 浏览器仓不存在: {ANDROID_REPO}")
    if not GRADLEW.exists():
        err(f"缺 {GRADLEW}（工程不完整）")


def java_major() -> int:
    txt = out(["bash", "-c", "java -version 2>&1 | head -1"])
    m = re.search(r'"(\d+)', txt)
    return int(m.group(1)) if m else 0


def jdk_major_at(p: Path) -> int:
    """目录级版本探测。java -version 写 stderr，必须重定向才抓得到。"""
    java = p / "bin" / "java"
    if not java.exists():
        return 0
    txt = out(["bash", "-c", f"{shlex.quote(str(java))} -version 2>&1 | head -1"])
    m = re.search(r'"(\d+)', txt)
    return int(m.group(1)) if m else 0


def is_full_jdk21(p: Path) -> bool:
    """Gradle 要的是能编译的 JDK：只装了 JRE（缺 javac）会被判成 JRE 拒收，
    转头按 toolchainUrl 去 foojay / github 下载。"""
    return (p / "bin" / "javac").exists() and jdk_major_at(p) == 21


def ensure_jdk():
    """gradle/gradle-daemon-jvm.properties 锁 toolchainVersion=21，且必须是完整 JDK。
    系统只装了 openjdk-21-jre 时，光看目录名 java-21* 会挑到 JRE —— Gradle 拒收后
    去外网下载，不通就失败；所以这里按 javac 是否存在筛选，并认 Gradle 自己供应过的那份。"""
    jh = os.environ.get("JAVA_HOME", "")
    if jh and is_full_jdk21(Path(jh)):
        return
    cands = []
    jvm = Path("/usr/lib/jvm")
    if jvm.is_dir():
        cands += sorted(jvm.glob("java-21*"))
    # Gradle 按 toolchainUrl 自动供应过的 JDK（~/.gradle/jdks）—— 系统只有 JRE 时靠它兜底
    gd = Path(os.environ.get("GRADLE_USER_HOME", str(Path.home() / ".gradle"))) / "jdks"
    if gd.is_dir():
        cands += sorted(p for p in gd.iterdir() if p.is_dir() and "21" in p.name)
    hits = [p for p in cands if is_full_jdk21(p)]
    if hits:
        os.environ["JAVA_HOME"] = str(hits[0])
        os.environ["PATH"] = str(hits[0] / "bin") + os.pathsep + os.environ.get("PATH", "")
        log(f"JDK: 切到 {hits[0]}（Gradle 要求 21，且必须是带 javac 的完整 JDK）")
        return
    v = java_major()
    if v == 21:
        warn(f"当前 java 是 21，但没找到带 javac 的完整 JDK —— Gradle 可能转头去外网下载 JDK\n"
             f"  装完整包: apt install -y openjdk-21-jdk")
        return
    warn(f"当前 JDK {v}，Gradle 要求 21 —— 大概率起不来。装: apt install -y openjdk-21-jdk\n"
         f"  （或让 Gradle 按 gradle/gradle-daemon-jvm.properties 的 toolchainUrl 自己下）")


def kernel_aar(c: Ctx) -> Path:
    return ANDROID_REPO / "app" / "libs" / "kernel" / KERNEL_ABI.get(c.arch, c.arch) / "arupa-kernel.aar"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_kernel_aar(c: Ctx):
    """AAR 缺失 / 指纹不符都会让 Gradle 在校验处炸 —— 提前点名，别等堆栈。"""
    p = kernel_aar(c)
    rel = p.relative_to(ANDROID_REPO).as_posix()
    if not p.exists():
        warn(f"内核 AAR 不存在: {rel} —— Gradle 会在 runtime-manifest 校验处失败\n"
             f"  内核编译 + 出 AAR 后拷到该路径，并同步 {RUNTIME_MANIFEST.name} 的 sha256")
        return
    if not RUNTIME_MANIFEST.exists():
        return
    man = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8", errors="replace"))
    want = (man.get("files") or {}).get(rel)
    if not want:
        return
    if _sha256(p) != want:
        warn(f"内核 AAR 指纹与 {RUNTIME_MANIFEST.name} 不符（清单记录交付 "
             f"{man.get('kernelDeliveryId')}）:\n  {rel}\n"
             f"  换内核后必须同步更新该清单的 sha256，否则 Gradle 拒绝构建")


def do_manifest(c: Ctx):
    """把本机 AAR 的实际 sha256 写回 runtime-manifest.json。

    Gradle 的 verifyMv3KernelDelivery 在配置期就比对指纹，不符直接失败并甩一句
    expected=.../actual=... —— 换内核后必须同步这里，否则 APK 根本起不来。
    注意只改指纹: kernelDeliveryId 描述的是"清单声称的内核交付"，真换内核时它也要跟着改。"""
    _require_repo()
    if not RUNTIME_MANIFEST.exists():
        err(f"清单不存在: {RUNTIME_MANIFEST}")
    man = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8", errors="replace"))
    files = man.setdefault("files", {})
    changed = []
    for abi in ("arm64", "x64"):
        p = ANDROID_REPO / "app" / "libs" / "kernel" / abi / "arupa-kernel.aar"
        rel = p.relative_to(ANDROID_REPO).as_posix()
        if not p.exists() or rel not in files:
            continue
        got = _sha256(p)
        if files[rel] == got:
            continue
        changed.append(f"  {rel}\n    - {files[rel]}\n    + {got}")
        files[rel] = got
    if not changed:
        log(f"{RUNTIME_MANIFEST.name} 指纹已与本机 AAR 一致，无需改动")
        return
    if common.DRY_RUN:
        log(f"(dry-run) 将更新 {RUNTIME_MANIFEST.name}:\n" + "\n".join(changed))
        return
    RUNTIME_MANIFEST.write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
    log(f"{RUNTIME_MANIFEST.name} 已同步为本机 AAR 的实际指纹:\n" + "\n".join(changed))
    warn(f"kernelDeliveryId 仍是 {man.get('kernelDeliveryId')} —— 真换内核时它要一起改，"
         f"否则清单声称的交付与实际 AAR 对不上")


def ensure_local_properties(d: Path):
    """仓库里那份 local.properties 是 Windows 路径（D:\\Android\\sdk）。不指到本机 SDK
    AGP 只丢一句 "Directory does not exist"，接着甩 license 未接受 —— 根因被掩盖。"""
    lp = ANDROID_REPO / "local.properties"
    cur = lp.read_text(encoding="utf-8", errors="replace") if lp.exists() else ""
    lines = [l for l in cur.splitlines() if not l.startswith("sdk.dir=")]
    lines.append(f"sdk.dir={d}")
    new = "\n".join(lines) + "\n"
    if new == cur:
        log(f"local.properties 已指向: {d}")
        return
    if common.DRY_RUN:
        log(f"(dry-run) 写 {lp}: sdk.dir={d}")
        return
    lp.write_text(new, encoding="utf-8")
    log(f"local.properties 已指向: {d}")


def do_down(c: Ctx):
    """装 Android SDK，并把 local.properties 指过去。"""
    _require_repo()
    import fetch
    fetch.DRY_RUN = common.DRY_RUN
    ensure_jdk()      # sdkmanager 本身要 java，先备好
    d = fetch.do_android_sdk(c.cfg)
    ensure_local_properties(d)


def _apply_gradle_proxy():
    """Gradle 是 JVM 进程，不认 http_proxy 环境变量 —— 不显式给 -D，wrapper 下
    gradle-*.zip 和 Gradle 拉 Maven 依赖都会连外网超时（10s 就放弃）。"""
    p = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
         or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or "")
    if not p:
        return
    m = re.match(r"(?:https?://)?(?:(?:[^:@/]+)(?::[^@/]*)?@)?([^:/]+)(?::(\d+))?",
                 p.strip())
    if not m:
        warn(f"代理地址解析不了（{p}）—— Gradle 直连，可能下载超时")
        return
    host, port = m.group(1), m.group(2) or "80"
    opts = (f"-Dhttp.proxyHost={host} -Dhttp.proxyPort={port} "
            f"-Dhttps.proxyHost={host} -Dhttps.proxyPort={port} "
            f"-Dhttp.nonProxyHosts=localhost|127.0.0.1")
    os.environ["GRADLE_OPTS"] = (os.environ.get("GRADLE_OPTS", "") + " " + opts).strip()
    log(f"Gradle 走代理 {host}:{port}（JVM 不读 http_proxy，需显式传 -D）")


def do_build(c: Ctx):
    _require_repo()
    ensure_jdk()
    _apply_gradle_proxy()
    import fetch
    fetch.DRY_RUN = common.DRY_RUN
    # 幂等: 没装就现装。原来只取路径不装 —— 用户直接跑 build（没跑 down）时 SDK 是
    # 空目录，AGP 会先报 "Directory does not exist" 再报 license，看不出是没装。
    sdk = fetch.do_android_sdk(c.cfg)
    ensure_local_properties(sdk)
    os.environ.setdefault("ANDROID_HOME", str(sdk))
    os.environ.setdefault("ANDROID_SDK_ROOT", str(sdk))
    check_kernel_aar(c)
    abi = KERNEL_ABI.get(c.arch, c.arch)
    task = "assembleDebug" if c.variant == "debug" else "assembleRelease"
    cmd = ["./gradlew", f":app:{task}", f"-PkernelAbi={abi}"]
    if c.jobs:
        cmd += ["--max-workers", str(c.jobs)]
    log(f"编译 APK（{c.variant} / {abi}）—— 首次要拉大量依赖，几十分钟量级")
    if not run(cmd, cwd=ANDROID_REPO):
        err("APK 编译失败")
    apk_dir = ANDROID_REPO / "app" / "build" / "outputs" / "apk" / c.variant
    for a in sorted(apk_dir.glob("*.apk")):
        log(f"APK: {a.name} ({human_size(a)})")


def do_package(c: Ctx):
    _require_repo()
    apk_dir = ANDROID_REPO / "app" / "build" / "outputs" / "apk" / c.variant
    apks = sorted(apk_dir.glob("*.apk")) if apk_dir.is_dir() else []
    if not apks:
        err(f"没有 APK: {apk_dir}（先跑: python3 scripts/build.py android build）")
    n = next_delivery_no(c)
    name = c.dist_name(n)
    stage = DIST_ROOT / name
    if stage.exists():
        err(f"交付包已存在，拒绝覆盖: {stage}（换 --delivery-n 或先清理）")
    if common.DRY_RUN:
        log(f"(dry-run) 将出交付包: {stage}（{len(apks)} 个 APK）")
        return None
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True, exist_ok=True)
    for a in apks:
        shutil.copy2(a, stage / a.name)
        log(f"  APK: {a.name} ({human_size(a)})")
    write_sha256sums(stage)
    purge_previous_deliveries(c, keep=name)
    record_delivery(c, n, stage, sorted(p.relative_to(stage).as_posix()
                                        for p in stage.rglob("*") if p.is_file()))
    log(f"交付包: {stage}")
    return stage


def run_android(c: Ctx, actions: list):
    for a in actions:
        if a == "down":
            do_down(c)
        elif a == "build":
            do_build(c)
        elif a == "package":
            do_package(c)
        elif a == "manifest":
            do_manifest(c)
