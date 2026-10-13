#!/usr/bin/env python3
"""桌面内核 C 头 ↔ 托管门面 ABI 面的『同源』保障（生成 / 校验 / 出包同步）。

背景（2026-10-11 static-25 实证）
--------------------------------
内核第八批加了 ARUPA_INTERCEPT_BLOCK_REQUEST = 2 与 request.block.v1：C 头里有了，
手写的托管门面 package/package_desktop/dotnet/ArupaBrowser.cs 没跟上。出包只是把
<repo>/package/package_desktop/dotnet 整份 cp 进交付（连侧车 .facade-source.sha256 一起），
而那份侧车只覆盖门面自己的 *.cs —— "门面与源码自洽"恒成立，kernel_facade 判 current 跳过
重建，于是浏览器编译时才炸 CS0117: InterceptAction 未包含 BlockRequest 的定义。每次内核
更新都要人肉补 C#、重编门面 DLL，返工点全部落在"由 C 头机械决定的常量"上。

做法
----
把这类常量改成**只能由 C 头派生**（与内核仓 Android 侧 scripts/kernel_capability.py 的
"Java 门面 ↔ C++ 结果码同源"同一口径）：

    生成   python scripts/gen_arupa_desktop_abi.py                   # 刷新仓内模板
    校验   python scripts/gen_arupa_desktop_abi.py --check           # 不一致 exit 1
    出包   python scripts/gen_arupa_desktop_abi.py --stage <交付根>   # 现场生成 + 重建门面 DLL

生成物是 dotnet/ArupaAbi.g.cs（仓内: package/package_desktop/dotnet/ArupaAbi.g.cs）。
手写文件里对应的枚举声明**已删除** —— 一个类型只能有一个定义，两边不能同时留。
托管成员名与 C 名不一致的历史公开 API 在 VALUE_ALIASES 里显式登记：例外要写下来，不靠猜。

边界
----
只覆盖由 C 头机械决定的东西（枚举、常量、通用能力探针）。新增 P/Invoke 导出函数与带类型的
新方法仍然要手写 —— 那部分没有可自动派生的语义，本模块不假装能生成。

自适应（2026-10-12 实证）
------------------------
托管 wrapper（`ArupaBrowser.cs`）的源头在内核侧：他们批 11 的版本又把 `InterceptAction` /
`BrowserCommand` 内联回了文件里。此时生成物若照旧声明同名枚举，就是 CS0101/CS0111 重复定义。
所以生成物按**同目录 wrapper 的实际布局**自适应：

    wrapper 里没有该枚举 → 生成（内核加值零改动）
    wrapper 里有该枚举   → 不生成，但把它的成员与 C 头逐个核对：
                           名字或值不一致 → 生成/校验直接失败

于是"内核加了值、门面没跟上"这件事无论走哪条路都会被拦住：生成式的自动带上，内联式的在
出包/构建早期报出精确差异。另外 dotnet/ 里出现**重复的门面副本**（典型是手工复制留下的
`ArupaBrowser copy.cs`）也在这里拦：C# 工程 glob `*.cs`，多一份就是 CS0101/CS0111。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from xml.sax.saxutils import escape as xml_escape

WORKSPACE = Path(__file__).resolve().parents[1]

# 仓内模板：内核源码在 arupa_desktop/，门面模板在 package/package_desktop/dotnet/。
GENERATED_NAME = "ArupaAbi.g.cs"
HEADER_IN_DELIVERY = Path("include") / "arupa_kernel_capi.h"
WRAPPER_NAME = "ArupaBrowser.cs"
TEMPLATE_HEADER = WORKSPACE / "arupa_desktop" / "public" / "arupa_kernel_capi.h"
TEMPLATE_DOTNET = WORKSPACE / "package" / "package_desktop" / "dotnet"
TEMPLATE_GENERATED = TEMPLATE_DOTNET / GENERATED_NAME

# 交付里"门面之外"的输入（kernel_facade 的摘要也认这两个，见该模块 KERNEL_CONTEXT_FILES）。
KERNEL_CONTEXT_FILES = (HEADER_IN_DELIVERY, Path("kernel") / ".arupa-version")


def log(msg):
    print("[INFO] " + str(msg), file=sys.stderr, flush=True)


def warn(msg):
    print("[WARN] " + str(msg), file=sys.stderr, flush=True)


def err(msg):
    print("[ERROR] " + str(msg), file=sys.stderr, flush=True)
    sys.exit(1)


class GenError(RuntimeError):
    pass


@dataclass(frozen=True)
class Member:
    c_name: str
    value: int
    doc: str = ""


@dataclass(frozen=True)
class Facts:
    source_name: str
    abi_major: int
    abi_minor: int
    enums: dict = field(default_factory=dict)     # C enum 名 -> [Member]
    defines: dict = field(default_factory=dict)   # 宏名 -> Member


@dataclass(frozen=True)
class EnumSpec:
    """一个 C 头来源 → 一个托管枚举。"""
    cs_name: str
    summary: str
    source: str                 # "enum" | "define"
    c_name: str = ""            # source == "enum" 时的 C enum 名
    prefix: str = ""            # 成员名前缀（去掉后 PascalCase）


# 由 C 头派生的托管 ABI 面。加新枚举 = 这里加一行 + 重跑生成；
# 手写文件里对应的声明必须同时删掉（重复定义会当场编译失败）。
ENUM_SPECS = (
    EnumSpec(
        cs_name="InterceptAction",
        summary="拦截回调的处置方式 (capi.h ArupaInterceptAction / ArupaInterceptResponse.action)。",
        source="enum",
        c_name="ArupaInterceptAction",
        prefix="ARUPA_INTERCEPT_",
    ),
    EnumSpec(
        cs_name="BrowserCommand",
        summary="窗口动作 (capi.h ARUPA_BROWSER_COMMAND_*)；与网页 HTML 全屏的 FullscreenChanged 独立。",
        source="define",
        prefix="ARUPA_BROWSER_COMMAND_",
    ),
    EnumSpec(
        cs_name="ArupaKernelState",
        summary="内核生命周期 (capi.h ArupaKernelState)；一次进程一次，stopped/failed 只能重启进程。",
        source="enum",
        c_name="ArupaKernelState",
        prefix="ARUPA_KERNEL_",
    ),
)

# 托管成员名 ≠ "C 名去前缀后 PascalCase" 的历史公开 API 在这里登记（键: (托管枚举名, C 成员名)）。
# 这些名字已经被宿主/第三方引用，不能为了生成器顺手改名。
VALUE_ALIASES = {
    ("InterceptAction", "ARUPA_INTERCEPT_MODIFY_HEADERS"): "ContinueWithModifiedHeaders",
    ("BrowserCommand", "ARUPA_BROWSER_COMMAND_OPEN_DEVTOOLS"): "OpenDevTools",
}

_DEFINE_MAJOR = re.compile(r"^\s*#define\s+ARUPA_KERNEL_ABI_MAJOR\s+(\d+)\s*$", re.M)
_DEFINE_MINOR = re.compile(r"^\s*#define\s+ARUPA_KERNEL_ABI_MINOR\s+(\d+)\s*$", re.M)
_DEFINE_VALUE = re.compile(r"^\s*#define\s+([A-Z][A-Z0-9_]*)\s+(-?\d+)\s*(?://(.*))?$")
_ENUM_OPEN = re.compile(r"^\s*typedef\s+enum\s*(\w+)?\s*\{\s*$")
_ENUM_CLOSE = re.compile(r"^\s*\}\s*(\w+)?\s*;\s*(?://.*)?$")
_ENUM_MEMBER = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)\s*,?\s*(?://(.*))?$")


def parse_header(text: str, source_name: str) -> Facts:
    """从 capi.h 文本里取 ABI 版本、枚举、整数宏。只做"头文件里写死了的东西"。"""
    major = _DEFINE_MAJOR.search(text)
    minor = _DEFINE_MINOR.search(text)
    if not major or not minor:
        raise GenError(f"{source_name} 里找不到 ARUPA_KERNEL_ABI_MAJOR/MINOR")
    enums: dict = {}
    defines: dict = {}
    comments: list = []
    current: Optional[tuple] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if current is not None:
            close = _ENUM_CLOSE.match(line)
            if close:
                name = current[0] or (close.group(1) or "")
                if name:
                    enums[name] = current[1]
                elif current[1]:
                    raise GenError(f"{source_name}: 有名成员的匿名枚举没写别名，无法派生托管名")
                current = None
                comments = []
                continue
            member = _ENUM_MEMBER.match(line)
            if member:
                doc = " ".join(comments) or (member.group(3) or "").strip()
                current[1].append(Member(member.group(1), int(member.group(2)), doc))
                comments = []
                continue
            if stripped.startswith("//"):
                comments.append(stripped[2:].strip())
            continue
        opened = _ENUM_OPEN.match(line)
        if opened:
            current = (opened.group(1), [])
            comments = []
            continue
        if stripped.startswith("//"):
            comments.append(stripped[2:].strip())
            continue
        macro = _DEFINE_VALUE.match(line)
        if macro:
            defines[macro.group(1)] = Member(macro.group(1), int(macro.group(2)), (macro.group(3) or "").strip())
            comments = []
            continue
        comments = []
    return Facts(source_name=source_name, abi_major=int(major.group(1)), abi_minor=int(minor.group(1)),
                 enums=enums, defines=defines)


def pascal(stem: str) -> str:
    return "".join(part.capitalize() for part in stem.split("_") if part)


def spec_members(spec: EnumSpec, facts: Facts) -> list:
    """[(托管成员名, 值, C 名, 文档)]；缺源或缺成员都当生成源不同步，直接失败。"""
    if spec.source == "enum":
        raw = facts.enums.get(spec.c_name)
        if raw is None:
            raise GenError(f"{facts.source_name}: 找不到 enum {spec.c_name}（{spec.cs_name} 的生成源）")
        pool = list(raw)
    else:
        pool = sorted((m for name, m in facts.defines.items() if name.startswith(spec.prefix)),
                      key=lambda m: m.value)
        if not pool:
            raise GenError(f"{facts.source_name}: 找不到 {spec.prefix}* 宏（{spec.cs_name} 的生成源）")
    out = []
    seen = {}
    for member in pool:
        if not member.c_name.startswith(spec.prefix):
            raise GenError(f"{facts.source_name}: {member.c_name} 不属于 {spec.prefix}（{spec.cs_name}）")
        cs_name = VALUE_ALIASES.get((spec.cs_name, member.c_name)) or pascal(member.c_name[len(spec.prefix):])
        if not cs_name:
            raise GenError(f"{facts.source_name}: {member.c_name} 派不出托管名")
        if cs_name in seen:
            raise GenError(f"{spec.cs_name}: {member.c_name} 与 {seen[cs_name]} 都派生成 {cs_name}；"
                           "在 VALUE_ALIASES 里给其中之一显式命名")
        seen[cs_name] = member.c_name
        out.append((cs_name, member.value, member.c_name, member.doc))
    return out


def render(facts: Facts, wrapper_text: Optional[str] = None) -> str:
    """确定性渲染（同一份 C 头 + 同一份 wrapper → 同一个字节序列；校验就是逐字节比对）。

    wrapper_text 非空时按 wrapper 的实际布局自适应：里面已内联的枚举不再生成，但逐值核对
    （见模块头"自适应"一节）。
    """
    inline = parse_cs_enums(wrapper_text) if wrapper_text else {}
    verified = sorted(name for name in inline if any(spec.cs_name == name for spec in ENUM_SPECS))
    preamble = [
        "// <auto-generated>",
        f"//   由 scripts/gen_arupa_desktop_abi.py 从内核 C 头生成（源: {facts.source_name}），请勿手改。",
        f"//   ABI {facts.abi_major}.{facts.abi_minor}。wrapper 里已内联的枚举只做数值核对、不重复生成；",
        "//   其余 ABI 面由本文件提供 —— 内核加值走哪条路都会被拦住或自动带上。",
    ]
    if verified:
        preamble.append(f"//   已按 {facts.source_name} 核对通过的内联枚举: {', '.join(verified)}")
    preamble += [
        "//",
        "//   重新生成: python scripts/gen_arupa_desktop_abi.py",
        "//   同源校验: python scripts/gen_arupa_desktop_abi.py --check",
        "// </auto-generated>",
    ]

    body = []
    if not (wrapper_text and "class ArupaAbi" in wrapper_text):
        body += [
            f"    /// <summary>Arupa 内核 ABI {facts.abi_major}.{facts.abi_minor}：由 C 头决定的常量（同源生成）。</summary>",
            "    public static class ArupaAbi",
            "    {",
            "        public const int Major = %d;" % facts.abi_major,
            "        public const int Minor = %d;" % facts.abi_minor,
            "    }",
            "",
        ]
    for spec in ENUM_SPECS:
        expected = spec_members(spec, facts)
        declared = inline.get(spec.cs_name)
        if declared is not None:
            wanted = {name: value for name, value, _c_name, _doc in expected}
            if declared != wanted:
                raise GenError(enum_drift(facts.source_name, spec.cs_name, declared, expected))
            continue                       # wrapper 自己声明了：不重复生成（值已核对）
        body.append(f"    /// <summary>{xml_escape(spec.summary)}</summary>")
        body.append(f"    public enum {spec.cs_name}")
        body.append("    {")
        for cs_name, value, c_name, doc in expected:
            summary = f"{c_name} = {value}" + (f" — {doc}" if doc else "")
            body.append(f"        /// <summary>{xml_escape(summary)}</summary>")
            body.append(f"        {cs_name} = {value},")
        body.append("    }")
        body.append("")
    if not (wrapper_text and "bool Supports(" in wrapper_text):
        if wrapper_text and "partial class ArupaKernel" not in wrapper_text:
            raise GenError(
                "ArupaBrowser.cs 的 ArupaKernel 必须声明成 partial：通用能力探针 "
                "Supports(string) 由生成物补进去（见 scripts/gen_arupa_desktop_abi.py）。"
                "把 `sealed class ArupaKernel` 改成 `sealed partial class ArupaKernel` 即可。")
        body += [
            "    /// <summary>",
            "    /// ArupaKernel 的生成部分（手写文件的 partial 在这里补齐 ABI 面）。",
            "    /// </summary>",
            "    public sealed partial class ArupaKernel",
            "    {",
            "        /// <summary>",
            "        /// 通用能力探针: arupa_kernel_supports(feature) (capi.h / capabilities.v1)。",
            "        /// 内核新增能力时宿主直接传能力名即可 —— 不必为每个能力再加一个 SupportsXxx 属性，",
            "        /// 于是「内核更新 → 门面要改」的另一半返工也一并消失。",
            "        /// 老内核没有该导出时返回 false，不抛。",
            "        /// </summary>",
            "        public static bool Supports(string feature)",
            "        {",
            "            if (string.IsNullOrEmpty(feature)) return false;",
            "            try { return Interop.arupa_kernel_supports(feature) != 0; }",
            "            catch (EntryPointNotFoundException) { return false; }",
            "        }",
            "    }",
        ]
        body.append("")
    if not body:
        # wrapper 全包了（且已核对）：生成物退化成一份"我核对过"的凭据，不留半个 namespace。
        preamble.append(f"//   本交付的 ABI 面全部由 {WRAPPER_NAME} 内联提供，且已与 {facts.source_name} 核对通过。")
        return "\n".join(preamble) + "\n"
    lines = preamble + ["using System;", "", "namespace Arupa", "{"] + body[:-1] + ["}"]
    return "\n".join(lines) + "\n"


_CS_ENUM = re.compile(r"\benum\s+([A-Za-z_]\w*)\s*\{")
_CS_MEMBER = re.compile(r"([A-Za-z_]\w*)\s*=\s*(-?\d+)")


def _cs_block(text: str, open_index: int) -> str:
    """取 `{` 的配对块内容（枚举体里没有嵌套花括号，够用且不引依赖）。"""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:index]
    return text[open_index + 1:]


def parse_cs_enums(text: str) -> dict:
    """wrapper 里内联声明的枚举: {枚举名: {成员: 值}}；无显式赋值的成员按 C# 规则顺延。"""
    found = {}
    for match in _CS_ENUM.finditer(text):
        body = _cs_block(text, match.end() - 1)
        members = {}
        next_value = 0
        for token in re.split(r"[,\n]", body):
            token = token.split("//")[0].strip()
            if not token:
                continue
            pair = _CS_MEMBER.fullmatch(token)
            if pair:
                members[pair.group(1)] = int(pair.group(2))
                next_value = int(pair.group(2)) + 1
            elif re.fullmatch(r"[A-Za-z_]\w*", token):
                members[token] = next_value
                next_value += 1
        found[match.group(1)] = members
    return found


def enum_drift(header_name: str, cs_name: str, declared: dict, expected: list) -> str:
    """内联枚举与 C 头不一致时的可读说明：缺谁、多谁、谁的值不一样。"""
    wanted = {name: value for name, value, _c_name, _doc in expected}
    rows = []
    for name, value in sorted(((n, v) for n, v in wanted.items() if n not in declared),
                              key=lambda item: item[1]):
        rows.append(f"      缺 {name} = {value}（{header_name} 里有）")
    for name, value in sorted(((n, v) for n, v in declared.items() if n not in wanted),
                              key=lambda item: item[1]):
        rows.append(f"      多 {name} = {value}（{header_name} 里没有）")
    for name in sorted(n for n in declared if n in wanted and declared[n] != wanted[n]):
        rows.append(f"      值不符 {name}: 门面 {declared[name]} ≠ {header_name} {wanted[name]}")
    return (f"wrapper 内联的 {cs_name} 与 {header_name} 不一致（内核动了 ABI 面而门面没跟上）：\n"
            + "\n".join(rows)
            + "\n      改法: 更新 ArupaBrowser.cs 里的该枚举，或整段删掉改由生成物提供")


def read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Path(path).read_text(encoding="utf-8", errors="replace")


def wrapper_beside(generated) -> Optional[Path]:
    """生成物同目录的 wrapper —— 自适应布局的判据就在这里（两者必须一起搬）。"""
    candidate = Path(generated).parent / WRAPPER_NAME
    return candidate if candidate.is_file() else None


def wrapper_needs_generated(wrapper_text: str) -> bool:
    """wrapper 自己不够用时才需要生成物（缺枚举 / 缺 ArupaAbi 常量 / 缺通用探针）。"""
    inline = parse_cs_enums(wrapper_text)
    if any(spec.cs_name not in inline for spec in ENUM_SPECS):
        return True
    return "class ArupaAbi" not in wrapper_text or "bool Supports(" not in wrapper_text


def expected_text(header: Path, wrapper=None) -> str:
    if not header.is_file():
        raise GenError(f"找不到内核 C 头: {header}")
    wrapper_text = read_text(wrapper) if wrapper is not None and Path(wrapper).is_file() else None
    return render(parse_header(read_text(header), header.name), wrapper_text)


def diff_text(expected: str, actual: str, name: str, limit: int = 40) -> str:
    diff = list(difflib.unified_diff(actual.splitlines(), expected.splitlines(),
                                     fromfile=f"{name}（现文件）", tofile=f"{name}（C 头派生）", lineterm=""))
    if len(diff) > limit:
        diff = diff[:limit] + [f"...（diff 截断，共 {len(diff)} 行）"]
    return "\n".join(diff)


def write_generated(header: Path, generated: Path, check_only: bool = False) -> bool:
    """返回"是否一致"。check_only 时不写盘。wrapper 取生成物同目录的那一份。"""
    expected = expected_text(header, wrapper_beside(generated))
    actual = read_text(generated) if generated.is_file() else None
    if actual == expected:
        return True
    if check_only:
        return False
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(expected, encoding="utf-8", newline="\n")
    log(f"已生成 {generated}（源: {header}）" if actual is None else f"已刷新 {generated}（源: {header}）")
    return True


# ── 与出包 / 浏览器构建共用的两个判据 ────────────────────────────────────────

def duplicate_wrappers(dotnet_dir) -> list:
    """dotnet/ 里重复定义门面类型的 .cs（典型: 手工复制留下的 "ArupaBrowser copy.cs"）。

    C# 工程是 glob `*.cs`：多一份就报 CS0101/CS0111 重复类型/成员，而报错位置指向主文件，
    看着像主文件写错了（2026-10-12 实证：包 26 就栽在这份 " copy" 上）。这里提前点出来。
    """
    directory = Path(dotnet_dir)
    if not directory.is_dir():
        return []
    problems = []
    for path in sorted(directory.glob("*.cs")):
        if path.name in (WRAPPER_NAME, GENERATED_NAME):
            continue
        text = read_text(path)
        if "class ArupaWebView" in text or "class ArupaKernel" in text:
            problems.append(
                f"dotnet/{path.name} 又定义了一份门面类型（ArupaWebView/ArupaKernel）—— "
                f"与 dotnet/{WRAPPER_NAME} 重复，C# 工程 glob 到它必炸 CS0101/CS0111。"
                "手工同步 wrapper 时请覆盖同名文件，不要留下 \" copy\" 副本")
    return problems


def parity_refusals(delivery_root: Path) -> list:
    """阻塞级问题：门面副本重复 / 生成物与交付自带 C 头不同源 / 内联枚举与 C 头不符。"""
    root = Path(delivery_root)
    header = root / HEADER_IN_DELIVERY
    generated = root / "dotnet" / GENERATED_NAME
    wrapper = root / "dotnet" / WRAPPER_NAME
    duplicates = duplicate_wrappers(root / "dotnet")
    if not header.is_file() or not wrapper.is_file():
        return duplicates               # 不是本模块管得着的交付（缺件由各自的必需件门禁报）
    wrapper_text = read_text(wrapper)
    if not generated.is_file():
        if not wrapper_needs_generated(wrapper_text):
            return duplicates           # wrapper 自给自足（且下面会逐值核对），生成物可有可无
        # wrapper 没内联全 → 还需要生成物。但先做内联核对：若它内联的那部分本身已经落后于
        # C 头，"门面 ABI 面落后"才是根因，别让"缺生成物"把它遮住（2026-10-12 批 11 就是这么来的）。
        try:
            expected_text(header, wrapper)
        except GenError as error:
            return duplicates + [str(error)]
        return duplicates + [
            f"门面缺生成物 dotnet/{GENERATED_NAME}，而 dotnet/{WRAPPER_NAME} 又没内联全部 ABI 面"
            "（会以 CS0246/CS0117 收场）。重出内核交付包"
            "（scripts/packaging/package-arupa_desktop.* 会自动生成），"
            f"或就地执行: python scripts/gen_arupa_desktop_abi.py --stage {root}"]
    try:
        expected = expected_text(header, wrapper)
    except GenError as error:
        return duplicates + [str(error)]
    actual = read_text(generated)
    if actual == expected:
        return duplicates
    return duplicates + [
        f"dotnet/{GENERATED_NAME} 与交付自带的 {HEADER_IN_DELIVERY}（+ 同目录 wrapper 的布局）不同源 —— "
        "该交付的门面是按另一份 C 头生成的，宿主按它编译等于赌 ABI：\n"
        + diff_text(expected, actual, GENERATED_NAME)
        + f"\n修复: python scripts/gen_arupa_desktop_abi.py --stage {root}"]


def parity_warnings(delivery_root: Path) -> list:
    """非阻塞提示：能编，但值得知道的事（全内联 wrapper 等）。"""
    root = Path(delivery_root)
    wrapper = root / "dotnet" / WRAPPER_NAME
    if not (root / HEADER_IN_DELIVERY).is_file() or not wrapper.is_file():
        return []
    if (root / "dotnet" / GENERATED_NAME).is_file():
        return []
    if wrapper_needs_generated(read_text(wrapper)):
        return []
    return [f"该交付的 ABI 面全部内联在 dotnet/{WRAPPER_NAME}（无 dotnet/{GENERATED_NAME}）："
            "已按交付自带的 C 头核对通过，本次沿用"]


# ── --stage：出包时把生成物与门面 DLL 一起落到交付里 ────────────────────────

def find_dotnet() -> Optional[str]:
    """按仓内约定解析 .NET SDK：配置 dotnet_path → 工作区 .tools/dotnet → PATH → 常见安装位置。

    不能直接用 PATH 上的 dotnet：门面工程（dotnet/ArupaKernel.csproj）目标是 net10.0，
    而机器上 PATH 的 dotnet 可能只有旧 SDK（本机 /usr/local/share/dotnet 只有 6.0.301，
    工作区 .tools/dotnet 才是配套的 10.0.401）。选错 SDK 的后果不是报错，是 NETSDK1045
    之后门面 DLL 静默建不出来、交付退化成"只带源码"，宿主只能等构建期自愈。
    口径与 scripts/toolchains.py:dotnet_executable 保持一致（单一来源）。
    """
    if str(WORKSPACE / "scripts") not in sys.path:
        sys.path.insert(0, str(WORKSPACE / "scripts"))
    try:
        import fetch as workspace_fetch                              # noqa: PLC0415
        import toolchains                                             # noqa: PLC0415
        executable = toolchains.dotnet_executable(workspace_fetch.load_config())
        resolved = shutil.which(executable) or executable
        if Path(resolved).is_file():
            return resolved
    except Exception:                                                # noqa: BLE001
        pass                                                          # 读配置失败不阻断出包，退回下面的探测
    found = shutil.which("dotnet")
    if found:
        return found
    for candidate in (r"C:\Program Files\dotnet\dotnet.exe", "/usr/local/share/dotnet/dotnet",
                      "/usr/share/dotnet/dotnet"):
        if Path(candidate).is_file():
            return candidate
    return None


def stage(delivery_root: Path, cpu: str, cfg: str, build: bool) -> int:
    root = Path(delivery_root).resolve()
    dotnet_dir = root / "dotnet"
    header = root / HEADER_IN_DELIVERY
    if not dotnet_dir.is_dir():
        warn(f"{root} 里没有 dotnet/（该交付不携带托管门面），跳过 ABI 面同步")
        return 0
    duplicates = duplicate_wrappers(dotnet_dir)
    if duplicates:
        for problem in duplicates:
            err(problem)               # 出包期就拒：重复副本编不过，别带进交付
    if not header.is_file():
        err(f"{root} 里没有 {HEADER_IN_DELIVERY}；请确认交付根路径（--stage 要的是交付根，不是 dotnet/）")
    if not write_generated(header, dotnet_dir / GENERATED_NAME):
        raise GenError("生成物写入失败")

    # 门面 DLL 必须由"它自己旁边的源码（含刚生成的 ABI 面）"编出来；
    # 交付的侧车摘要里还含本交付的 C 头，所以内核一升级这里必然重建。
    if build:
        sys.path.insert(0, str(WORKSPACE / "scripts"))
        import kernel_facade                                     # noqa: PLC0415 —— 只在出包路径上需要

        dotnet = find_dotnet()
        props = []
        nuget = WORKSPACE / "build" / "nuget.config"
        if nuget.is_file():
            props.append(f"-p:RestoreConfigFile={nuget}")
        result = kernel_facade.ensure(
            dotnet_dir, dotnet, cfg=cfg, arch=cpu or None, extra_props=props,
            runner=lambda command, cwd: subprocess.run(command, cwd=cwd).returncode == 0,
            cwd=str(WORKSPACE))
        if result.state == "failed":
            warn(f"交付门面未能重建（{result.detail}）—— 交付只带源码，PC 侧构建会现场自愈")
        elif result.state == "current":
            log(f"交付门面与源码一致（{kernel_facade.ASSEMBLY_NAME}）")
        else:
            log(f"交付门面已重建: {result.path}")
    problems = parity_refusals(root)
    if problems:
        for problem in problems:
            err(problem)
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="内核 C 头 ↔ 托管门面 ABI 面同源（生成/校验/出包同步）")
    parser.add_argument("--check", action="store_true", help="只校验生成物与 C 头是否同源（不一致 exit 1）")
    parser.add_argument("--stage", metavar="DELIVERY", help="把生成物落进交付并重建门面 DLL")
    parser.add_argument("--header", metavar="FILE", help="C 头路径（默认: 交付里 include/… 或仓内模板）")
    parser.add_argument("--generated", metavar="FILE", help="生成物路径（默认: dotnet/ArupaAbi.g.cs）")
    parser.add_argument("--cpu", default="", help="--stage 重建门面时的 Platform（如 x64）")
    parser.add_argument("--config", default="Release", help="--stage 重建门面的配置（默认 Release）")
    parser.add_argument("--no-build", action="store_true", help="--stage 只生成，不重建门面 DLL")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.stage:
            return stage(Path(args.stage), args.cpu, args.config, build=not args.no_build)

        header = Path(args.header) if args.header else TEMPLATE_HEADER
        generated = Path(args.generated) if args.generated else TEMPLATE_GENERATED
        if args.check:
            if write_generated(header, generated, check_only=True):
                log(f"{generated} 与 {header} 同源")
                return 0
            expected = expected_text(header, wrapper_beside(generated))
            actual = read_text(generated) if generated.is_file() else ""
            err(f"{generated} 与 {header} 不同源 —— 内核动了 ABI 面而门面没跟上（或反之）：\n"
                + diff_text(expected, actual, generated.name)
                + "\n修复: python scripts/gen_arupa_desktop_abi.py（然后重编 package/package_desktop/dotnet 的门面 DLL）")
        write_generated(header, generated)
        return 0
    except GenError as error:
        err(str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
