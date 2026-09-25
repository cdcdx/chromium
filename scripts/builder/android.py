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
import shutil
from pathlib import Path

from .common import (ANDROID_REPO, DIST_ROOT, Ctx, err, log, out, run, warn,
                     next_delivery_no, record_delivery, human_size,
                     purge_previous_deliveries, write_sha256sums)
from . import common

GRADLEW = ANDROID_REPO / "gradlew"
RUNTIME_MANIFEST = ANDROID_REPO / "tools" / "ci" / "runtime-manifest.json"
ACTIONS = ("down", "build", "package")

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


def ensure_jdk():
    """gradle/gradle-daemon-jvm.properties 锁 toolchainVersion=21 —— JDK 24 这类超前
    版本 Gradle 不认。没有 21 就先找系统里装过的，再退一步让 Gradle 按 toolchainUrl 自取。"""
    v = java_major()
    if v == 21:
        return
    jvm = Path("/usr/lib/jvm")
    hits = sorted(jvm.glob("java-21*")) if jvm.is_dir() else []
    if hits:
        os.environ["JAVA_HOME"] = str(hits[0])
        os.environ["PATH"] = str(hits[0] / "bin") + os.pathsep + os.environ.get("PATH", "")
        log(f"JDK: 切到 {hits[0]}（Gradle 要求 21，默认还是 {v}）")
        return
    warn(f"当前 JDK {v}，Gradle 要求 21 —— 大概率起不来。装: apt install -y openjdk-21-jdk\n"
         f"  （或让 Gradle 按 gradle/gradle-daemon-jvm.properties 的 toolchainUrl 自己下）")


def kernel_aar(c: Ctx) -> Path:
    return ANDROID_REPO / "app" / "libs" / "kernel" / KERNEL_ABI.get(c.arch, c.arch) / "arupa-kernel.aar"


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
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != want:
        warn(f"内核 AAR 指纹与 {RUNTIME_MANIFEST.name} 不符（清单记录交付 "
             f"{man.get('kernelDeliveryId')}）:\n  {rel}\n"
             f"  换内核后必须同步更新该清单的 sha256，否则 Gradle 拒绝构建")


def do_down(c: Ctx):
    """装 Android SDK，并把 local.properties 指过去（仓库里那份是 Windows 路径 D:\\Android\\sdk）。"""
    _require_repo()
    import fetch
    fetch.DRY_RUN = common.DRY_RUN
    d = fetch.do_android_sdk(c.cfg)
    ensure_jdk()
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


def do_build(c: Ctx):
    _require_repo()
    ensure_jdk()
    import fetch
    fetch.DRY_RUN = common.DRY_RUN
    sdk = fetch.android_sdk_dir(c.cfg)
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
