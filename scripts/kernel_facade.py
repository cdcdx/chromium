#!/usr/bin/env python3
"""交付内托管门面（ArupaKernel.dll / .xml）与它自带源码的一致性保障。

交付根 dotnet/ 是内核仓 package/package_desktop/dotnet 整份搬过去的：源码 + 产物。
产物一旦被当作"随包携带的文件"，就会连着出三类事故（2026-10-08 static-4 实证）：
  · 仓库里被 git 跟踪 —— 改了 .cs 忘了重编，DLL 悄悄停在旧版本；
  · 出包只做 cp -R —— 交付带着旧 DLL，还被写进 SHA256SUMS.txt；
  · Windows 宿主（NomadBrowser.Arupa.csproj 的 HintPath）直接引它 → CS0117/CS1061。

所以这里不猜"新不新"，而是把不变量写死：**门面必须由它旁边的源码编出来**。
源文件（*.cs + *.csproj）的 SHA-256 记在侧车 .facade-source.sha256 里，与 DLL
一一对应；对不上就重建，重建不出来就把过期产物删掉 —— 宁可让交付只带源码
（PC 侧脚本会现场自愈），也不能发一份与源码不符的门面。

判据用**内容摘要**而不是 mtime：交付是 cp -R 搬的，mtime 全是打包时刻，
"mtime 比源码新"在交付里恒真，按它判断等于永远不重建。
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

# ~4KB 的是 ref/reference-only 程序集（只有签名没有实现），引用它宿主会满屏
# CS0246 "未能找到 Arupa/ArupaWebView/ArupaKernel"。
MIN_FACADE_BYTES = 50_000

PROJECT_NAME = "ArupaKernel.csproj"
ASSEMBLY_NAME = "ArupaKernel.dll"
DOCS_NAME = "ArupaKernel.xml"
DIGEST_NAME = ".facade-source.sha256"
SOURCE_PATTERNS = ("*.cs", "*.csproj")
# 本模块在交付目录里就地 dotnet build，中间产物会落在 dotnet/bin、dotnet/obj；
# 交付不该带它们 —— 那里躺着的旧门面 DLL 正是"拿错门面"的温床（宿主的 HintPath
# 回落链 `dotnet\bin\x64\Release\net10.0\ArupaKernel.dll` 会扫到它）。产物归位后清掉。
INTERMEDIATE_DIRS = ("bin", "obj")

# dotnet 的 Platform 与内核侧 arch 的对应；AnyCPU 也不影响产物路径回落。
PLATFORM_FOR_ARCH = {"x64": "x64", "arm64": "ARM64", "x86": "x86"}

STATE_TEXT = {
    "no-project": "交付里没有门面工程（dotnet/ArupaKernel.csproj）—— 跳过",
    "current": "交付门面与源码一致",
    "rebuilt": "交付门面已按源码重建",
    "failed": "门面与源码不符且重建失败",
    "dry-run": "交付门面与源码不符 —— (dry-run) 未重建",
}

# runner(command, cwd) -> 是否成功。调用方注入（fetch.run 等），便于离线测试替换。
Runner = Callable[[Sequence[str], Optional[Path]], bool]


@dataclass(frozen=True)
class Result:
    state: str
    path: Optional[Path] = None
    detail: str = ""

    @property
    def text(self) -> str:
        base = STATE_TEXT.get(self.state, self.state)
        return f"{base}: {self.detail}" if self.detail else base


def project(dotnet_dir) -> Path:
    return Path(dotnet_dir) / PROJECT_NAME


def facade_ok(path) -> bool:
    p = Path(path)
    return p.is_file() and p.stat().st_size >= MIN_FACADE_BYTES


def source_digest(dotnet_dir) -> str:
    """门面源码摘要（文件名 + 内容），与机器、时间无关。"""
    d = Path(dotnet_dir)
    digest = hashlib.sha256()
    sources = sorted({p for pattern in SOURCE_PATTERNS for p in d.glob(pattern)})
    for path in sources:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def recorded_digest(dotnet_dir) -> str:
    try:
        return (Path(dotnet_dir) / DIGEST_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_current(dotnet_dir) -> bool:
    """门面存在，且确实由当前源码编出（侧车摘要一致）。"""
    d = Path(dotnet_dir)
    if not facade_ok(d / ASSEMBLY_NAME):
        return False
    recorded = recorded_digest(d)
    return bool(recorded) and recorded == source_digest(d)


def built_assembly(dotnet_dir, cfg: str, platform: Optional[str]) -> Optional[Path]:
    """找 dotnet build 的产物。Platform 目录命名随 SDK 版本变，所以最后兜底全扫。"""
    d = Path(dotnet_dir)
    candidates = []
    if platform:
        candidates.append(d / "bin" / platform / cfg / "net10.0" / ASSEMBLY_NAME)
    candidates.append(d / "bin" / cfg / "net10.0" / ASSEMBLY_NAME)
    candidates.extend(sorted(d.glob(f"bin/**/{ASSEMBLY_NAME}")))
    for candidate in candidates:
        if facade_ok(candidate):
            return candidate
    return None


def deploy(dotnet_dir, built) -> Path:
    """把编出来的门面归位到交付顶层，并写下源码摘要。"""
    d = Path(dotnet_dir)
    top = d / ASSEMBLY_NAME
    shutil.copyfile(built, top)
    docs = Path(built).with_suffix(".xml")
    if docs.is_file():
        shutil.copyfile(docs, d / DOCS_NAME)
    (d / DIGEST_NAME).write_text(source_digest(d), encoding="utf-8")
    return top


def drop_intermediates(dotnet_dir) -> list:
    """删掉交付里的构建中间产物（bin/obj），返回被删的目录名。

    ensure() 是**在交付目录里**构建的（这样才能"用交付自带的源码编出门面"），
    于是 bin/obj 会留在这里；交付只该有源码 + 门面产物 + 侧车摘要。
    """
    d = Path(dotnet_dir)
    removed = []
    for name in INTERMEDIATE_DIRS:
        path = d / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(name)
    return removed


def discard(dotnet_dir) -> list:
    """删掉与源码不符（或不可用）的门面产物，返回被删的文件名。

    宁可交付只带源码：PC 侧 ensure/replace 会现场自愈，而一份与源码不符的
    DLL 会让引用它的宿主在编译期报 CS0117/CS1061，且看上去像代码问题。
    """
    d = Path(dotnet_dir)
    removed = []
    for name in (ASSEMBLY_NAME, DOCS_NAME, DIGEST_NAME):
        path = d / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    return removed


def command_for(dotnet_dir, dotnet_exe: str, cfg: str, platform: Optional[str],
                extra_props: Iterable[str] = ()) -> list:
    """构建命令。

    🔴 绝不能给 `-o <门面工程自己的目录>`：OutputPath 落在工程目录里时，SDK 的
       DefaultItemExcludes 会把该目录下的 .cs 全部排除，只编译生成的 AssemblyInfo
       → 编出 4KB 空程序集、退出码仍是 0（静默）。产物落到工程 bin/ 下再拷回顶层。
    """
    command = [str(dotnet_exe), "build", str(project(dotnet_dir)), "-c", cfg]
    if platform:
        command += [f"-p:Platform={platform}"]
    command += [str(prop) for prop in extra_props]
    return command


def ensure(dotnet_dir, dotnet_exe, cfg: str = "Release", arch: Optional[str] = None,
           extra_props: Iterable[str] = (), runner: Optional[Runner] = None,
           cwd=None, dry_run: bool = False, drop_on_failure: bool = True) -> Result:
    """确保交付门面由它自带的源码编出。

    state: no-project / current / rebuilt / failed / dry-run。
    failed 时若 drop_on_failure 为真，过期产物已被删除（交付只留源码）。
    """
    d = Path(dotnet_dir)
    proj = project(d)
    if not proj.is_file():
        return Result("no-project", detail=str(d))
    if is_current(d):
        # 就地修复旧交付时，bin/obj 可能还在（本次没重建，也要清掉）
        drop_intermediates(d)
        return Result("current", path=d / ASSEMBLY_NAME)
    present = facade_ok(d / ASSEMBLY_NAME)
    reason = "门面与源码不符" if present else "门面缺失或只有 ref 程序集"
    if dry_run:
        return Result("dry-run", detail=f"{reason}；本应: "
                      + " ".join(command_for(d, dotnet_exe or "dotnet", cfg,
                                             PLATFORM_FOR_ARCH.get(arch or ""), extra_props)))
    if not dotnet_exe:
        return _fail(d, drop_on_failure, f"{reason}；本机没有可用的 dotnet")
    if runner is None:
        return _fail(d, drop_on_failure, f"{reason}；未提供命令执行器")
    platform = PLATFORM_FOR_ARCH.get(arch or "")
    command = command_for(d, dotnet_exe, cfg, platform, extra_props)
    try:
        ok = bool(runner(command, cwd))
    except Exception as error:                       # noqa: BLE001 —— 任何失败都不能带病出交付
        return _fail(d, drop_on_failure, f"{reason}；重建失败: {error}")
    built = built_assembly(d, cfg, platform) if ok else None
    if built is None:
        return _fail(d, drop_on_failure, f"{reason}；重建后没有可用的 {ASSEMBLY_NAME}")
    top = deploy(d, built)
    dropped = drop_intermediates(d)
    note = f"；已清理中间产物 {', '.join(dropped)}" if dropped else ""
    # 就地修复已出包的交付时，它随包带的清单会立刻失真（记的还是修复前的产物）。
    # 出包流程里写清单发生在本函数之后，所以正常不会命中这里。
    if (d.parent / "SHA256SUMS.txt").is_file():
        return Result("rebuilt", path=top,
                      detail="该交付的 SHA256SUMS.txt 仍记录修复前的产物，需要重出包刷新" + note)
    return Result("rebuilt", path=top, detail=note.lstrip("；"))


def _fail(dotnet_dir, drop: bool, detail: str) -> Result:
    if drop:
        removed = discard(dotnet_dir)
        detail += "；已删除过期产物: " + (", ".join(removed) if removed else "（本来就没有）")
    # 重建失败的 bin/obj 同样不该留在交付里
    drop_intermediates(dotnet_dir)
    return Result("failed", detail=detail)
