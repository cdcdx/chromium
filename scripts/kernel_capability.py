#!/usr/bin/env python3
"""内核能力清单与「声明 ↔ 实现同源」门禁（Android 内核交付用）。

背景：2026-10-08 的 +22 清单写过 `"nomad.invoke": true`，而当时内核既没有
nomadTransfer 的 schema 也没有对应路由 —— 声明与实现不同源。本模块把清单改成
**只能由源码派生**，并在出包前跑双向门禁；任一条不成立就 exit 1，拒绝出包。

出包脚本（scripts/packaging/package-arupa_android.sh）在算 SHA256SUMS.txt 之前调用：
    python3 scripts/kernel_capability.py manifest --stage <交付目录> --ver <ver> --num <n>

独立核查（不需要构建产物，CI / 沙箱都能跑）：
    python3 scripts/kernel_capability.py           # 打印从源码派生的事实
    python3 scripts/kernel_capability.py gate      # 只跑门禁
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SRC = WORKSPACE_ROOT / "src"


def log(msg):
    # 日志统一走 stderr：stdout 只留给数据（facts 的 JSON / manifest 的路径），
    # 这样 `python3 scripts/kernel_capability.py facts | jq ...` 才不会被日志污染。
    print("[INFO] " + str(msg), file=sys.stderr, flush=True)


def warn(msg):
    print("[WARN] " + str(msg), file=sys.stderr, flush=True)


def err(msg):
    print("[ERROR] " + str(msg), file=sys.stderr, flush=True)
    sys.exit(1)


# ── 事实来源（全部在 src/chrome/browser/arupa_android 下）────────────────────
ROOT = Path("chrome/browser/arupa_android")
# 单一来源登记表: 路由前缀/宿主事件/协议插件命名空间都从这里派生（不再散写在 C++ 表
# 与下方 py 里）。C++ 侧由 scripts/gen_arupa_plugin_registry.py 把它生成到
# arupa_plugin_registry.h；本模块也以它为准派生并做『生成头 ↔ 登记表同源』门禁。
REGISTRY_JSON = ROOT / "plugin/arupa_plugin_registry.json"
REGISTRY_HEADER = ROOT / "plugin/arupa_plugin_registry.h"
ROUTING_HEADER = ROOT / "plugin/arupa_ext_api_prefixes.h"
MV3_PERMISSION_MAP = ROOT / "mv3/arupa_mv3_permission_map.cc"
MV3_PERMISSION_MAP_H = ROOT / "mv3/arupa_mv3_permission_map.h"
# K-02：Java 自检门面（与 native 权限表做跨语言契约核对）。
JAVA_MV3_GUARD = ROOT / "mv3/android/java/src/co/arupa/mv3/ArupaMv3Guard.java"
# K-03/K-04：内核能力契约（单一来源派生的提交产物；三者由 `contract --write` 同批生成，
# 由 verify_contract_sync 门禁保证与源码同源）。
CONTRACT_SCHEMA_VERSION = 1
CONTRACT_JSON = ROOT / "mv3/arupa-kernel-contract.json"
CONTRACT_HEADER = ROOT / "mv3/arupa_kernel_contract.h"
CONTRACT_JAVA = ROOT / "mv3/android/java/src/co/arupa/mv3/ArupaKernelContract.java"
# AAR 内契约文件名（打包反向核对与运行期读取都认这个路径）。
CONTRACT_ASSET_NAME = "assets/arupa-kernel-contract.json"
LISTENER_REGISTRY = ROOT / "plugin/arupa_ext_listener_registry.cc"
WORKER_WINDOW_HEADER = ROOT / "plugin/arupa_worker_window.h"
WAKE_OUTBOX_HEADER = ROOT / "plugin/arupa_nomadtransfer_outbox.h"
WAKE_OUTBOX_SOURCE = ROOT / "plugin/arupa_nomadtransfer_outbox.cc"
SW_HOST_SOURCE = ROOT / "plugin/arupa_extension_sw_host.cc"
SELF_TEST_SOURCE = ROOT / "mv3/arupa_mv3_self_test.cc"
SELF_TEST_H = ROOT / "mv3/arupa_mv3_self_test.h"
# K-06：自检的 JNI 导出落点（结构化结果从这里交给 Java 门面）。
MV3_GUARD_JNI = ROOT / "mv3/mv3_guard_jni.cc"
JAVA_BRIDGE_FACADE = ROOT / "plugin/android/java/src/co/arupa/plugin/ArupaPluginBridge.java"
# declarativeNetRequest 的三处落点：对外声明(schema) / 内核自认(支持表) / 真正走得到(分发)。
DNR_SCHEMA = ROOT / "plugin/api/declarative_net_request.json"
DNR_RULES_HEADER = ROOT / "mv3/arupa_dnr_rules.h"
EXT_API_HOST = ROOT / "plugin/arupa_ext_api_host.cc"
# R-03：扩展重新启用后向**既有文档**补注入内容脚本。渲染侧上游
# ScriptInjectionManager::OnUserScriptsUpdated 只擦 pending injection、不重注入，
# 所以"停用 → 重新启用"必须由内核自己补，否则用户得刷新页面。这四个落点缺一个，
# 这条链就是"看着接了、其实没接"。
REINJECT_SOURCE = ROOT / "plugin/arupa_content_script_reinject.cc"
REINJECT_HEADER = ROOT / "plugin/arupa_content_script_reinject.h"
LOADER_BRIDGE_SOURCE = ROOT / "plugin/arupa_extension_loader_bridge.cc"
PLUGIN_BUILD_GN = ROOT / "plugin/BUILD.gn"
SW_HOST_SOURCE = ROOT / "plugin/arupa_extension_sw_host.cc"
SW_SERVICE_HOST_SOURCE = ROOT / "plugin/arupa_service_worker_host.cc"
# chrome.storage.local / session / onChanged 的落点：后端 + API 分发 + 宿主 + mojom 面。
PLUGIN_STORAGE_H = ROOT / "mv3/plugin_storage.h"
PLUGIN_STORAGE_CC = ROOT / "mv3/plugin_storage.cc"
STORAGE_HOST = ROOT / "plugin/arupa_plugin_storage_host.cc"
STORAGE_MOJOM = ROOT / "plugin/mojom/arupa_plugin_storage.mojom"
STORAGE_IPC = ROOT / "plugin/arupa_ipc_message_sender.cc"
ALARMS_SOURCE = ROOT / "plugin/arupa_alarms.cc"

# v2.4 协议契约（内核不得单边放大/缩小）：业务 ≤30s、最长租约 45s、一次租约一项。
# 改了这些数字，就必须同批改 App（NetdiskProductionWorkerWindows）与 spec。
CONTRACT_BUSINESS_BUDGET_MS = 30_000
CONTRACT_MAX_LEASE_MS = 45_000
CONTRACT_MAX_ITEMS = 1

# C++ 结果码 → Java 门面同名常量：名字不同，含义与取值必须 1:1。
WINDOW_CODE_ALIASES = {
    "kOk": "WINDOW_OK",
    "kBadArgs": "WINDOW_BAD_ARGS",
    "kCallerNotAllowed": "WINDOW_CALLER_NOT_ALLOWED",
    "kExtensionDisabled": "WINDOW_EXTENSION_DISABLED",
    "kNoPermission": "WINDOW_NO_PERMISSION",
    "kNoLiveWorker": "WINDOW_NO_LIVE_WORKER",
    "kWindowLimit": "WINDOW_LIMIT",
}
WAKE_CODE_ALIASES = {
    "kInvalidArgs": "WAKE_INVALID_ARGS",
    "kNoPermission": "WAKE_NO_PERMISSION",
    "kExtensionDisabled": "WAKE_EXTENSION_DISABLED",
    "kBadPayload": "WAKE_BAD_PAYLOAD",
}

# 清单声称「运行期事实可查」就必须真有这些 Java 门面入口 —— available 只能从它们来。
RUNTIME_FACT_ENTRYPOINTS = (
    "reserveWorkerWindow",
    "isWorkerWindowLive",
    "releaseWorkerWindow",
    "nomadTransferCapabilities",
    "fireNomadTransferWake",
    "ackNomadTransferWake",
    "pendingNomadTransferWakes",
    "nomadTransferRevision",
    "setNomadTransferAvailability",
)

# 交付件必须**显式**提供的 consumer keep 规则：App 的
# nomad_android/app/build.gradle::verifyMv3KernelDelivery 把 AAR 的 proguard.txt
# 规范成一行后逐条做子串匹配，所以通配规则不算满足更具体的那条。
# 2026-10-08 实际踩过：ArupaPluginHost 那条在某次重构里丢掉，出包毫无察觉，
# 直到 App 构建被拒才暴露（该 Service 按类名在 Manifest 里声明，混淆即起不来）。
REQUIRED_CONSUMER_KEEP = (
    "-keep class co.arupa.mv3.** { *; }",
    "-keep class co.arupa.plugin.** { *; }",
    "-keep class co.arupa.plugin.ArupaPluginHost { *; }",
    "-keep class org.jni_zero.GEN_JNI { *; }",
    "-keep class J.N { *; }",
)


# ── 基础工具 ────────────────────────────────────────────────────────────────
def _read_source(rel: Path) -> str:
    p = SRC / rel
    if not p.is_file():
        err(f"找不到 {rel.as_posix()} —— 能力清单必须由真实源码派生，缺文件不许出包。")
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """去掉 C++ 注释：表里/注释里都有中文说明，注释里的引号曾被当成表项。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _array_body(text: str, name: str, where: Path) -> str:
    m = re.search(re.escape(name) + r"\s*\[\]\s*=\s*\{(.*?)\};", text, re.S)
    if not m:
        err(f"内核表解析失败：{where.as_posix()} 里找不到 {name}。\n"
            f"  能力清单必须由真实表生成；解析不到就不许出包（别静默写一份看起来通过的清单）。")
    return m.group(1)


def _quoted_strings(block: str) -> list:
    return re.findall(r'"([^"\\]*)"', block)


def _check_table(name: str, entries: list, suffix: str = "") -> None:
    bad = [e for e in entries
           if not e or re.search(r"[\s\u4e00-\u9fff]", e or "") or (suffix and not e.endswith(suffix))]
    if bad:
        err(f"内核表 {name} 解析异常（疑似把注释当表项）：{bad[:5]}")


def _constexpr_ints(rel: Path) -> dict:
    text = _strip_comments(_read_source(rel))
    rows = {m.group(1): int(m.group(2).replace("'", "").replace("_", ""))
            for m in re.finditer(
                r"inline\s+constexpr\s+(?:int64_t|int|size_t)\s+(k\w+)\s*=\s*(-?[0-9][0-9_']*)",
                text)}
    if not rows:
        err(f"{rel.as_posix()} 里解析不到 inline constexpr 常量 —— 解析器与源码已漂移。")
    return rows


def _require_consts(rel: Path, names: tuple) -> dict:
    rows = _constexpr_ints(rel)
    missing = [n for n in names if n not in rows]
    if missing:
        err(f"{rel.as_posix()} 缺常量 {missing} —— 清单要报的字段在内核里找不到，"
            f"不允许拿默认值糊过去。")
    return rows


def _cpp_function_bodies(text: str) -> dict:
    """粗略抽出 `类型 名字(...) {…}` 的函数体（花括号配对）。

    只用于本仓自写的门禁检查（风格固定）。解析不到关键函数时宁可报错让人来修
    门禁，也不放过。"""
    # 形参里允许嵌套括号（异步回调形参 base::OnceCallback<void(bool)> callback
    # 曾因为里层那个 ')' 让整条签名匹配不上，函数体被整个漏掉 —— 门禁于是假报
    # "PurgeExtensionData 没有作废代次"，而调用其实一直都在）。最多两层嵌套够用，
    # 更深的结构（lambda 形参）本来也不该被当成函数定义。
    _nested = r"(?:[^;{}()]|\((?:[^;{}()]|\([^;{}()]*\))*\))*"
    bodies = {}
    for m in re.finditer(r"\n[A-Za-z_][\w:<>,\s\*&]*?\b(\w+)\s*\(\s*" + _nested +
                         r"\s*\)\s*\{", text):
        name = m.group(1)
        start = m.end() - 1
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    bodies[name] = text[start:i + 1]
                    break
    return bodies


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_prefix(prefixes: list, name: str) -> bool:
    return any(name.startswith(p) for p in prefixes)


# ── 源码事实 ────────────────────────────────────────────────────────────────
def _read_registry() -> dict:
    p = SRC / REGISTRY_JSON
    if not p.is_file():
        err(f"找不到单一来源登记表 {p.as_posix()} —— 路由/命名空间必须由登记表派生。")
    return json.loads(p.read_text(encoding="utf-8"))


def _registry_protocol_namespaces(reg: dict) -> list:
    return [pl["namespace"] for pl in reg["protocol_plugins"]]


def _registry_protocol_capabilities(reg: dict) -> dict:
    return {pl["namespace"]: pl["capability"] for pl in reg["protocol_plugins"]}


def _netdisk_schema_paths(reg: dict) -> list:
    # 各协议插件声明的 api schema 相对路径（由登记表派生，不再硬编码 nomad*.json）。
    return [ROOT / "plugin/api" / pl["schema"] for pl in reg["protocol_plugins"]]


def verify_registry_header_sync() -> None:
    """门禁: 生成头(arupa_plugin_registry.h)必须与登记表(JSON)逐表同源。

    单源原则的兜底: 谁漏跑生成器、只改了 JSON，谁的出包在这里被拒，不让
    C++ 与 json 两份不一致的"事实"悄悄混进交付。"""
    reg = _read_registry()
    text = _strip_comments(_read_source(REGISTRY_HEADER))
    pairs = [
        ("kHostOwnedPrefixes", reg["routing"]["host_owned_prefixes"]),
        ("kKernelOwnedBridgePrefixes", reg["routing"]["kernel_owned_bridge_prefixes"]),
        ("kHostFireableEvents", reg["routing"]["host_fireable_events"]),
    ]
    for name, want in pairs:
        got = _quoted_strings(_array_body(text, name, REGISTRY_HEADER))
        if got != want:
            err(f"生成头 {REGISTRY_HEADER.as_posix()} 与登记表不同源: {name} 不一致。\n"
                f"  请重跑 python3 scripts/gen_arupa_plugin_registry.py 再出包。")


def kernel_routing_facts() -> dict:
    """从单一来源登记表(arupa_plugin_registry.json)读出真实路由/事件事实。"""
    verify_registry_header_sync()
    reg = _read_registry()
    routing = reg["routing"]
    host = routing["host_owned_prefixes"]
    kernel = routing["kernel_owned_bridge_prefixes"]
    events = routing["host_fireable_events"]
    _check_table("host_owned_prefixes", host, suffix=".")
    _check_table("kernel_owned_bridge_prefixes", kernel)
    _check_table("host_fireable_events", events)
    if not host or not events:
        err("登记表路由解析结果为空 —— 登记表与解析器已漂移，先修登记表再出包。")
    protos = _registry_protocol_namespaces(reg)
    proto_suffixes = {ns + "." for ns in protos}
    netdisk_prefixes = [p for p in host if p in proto_suffixes]
    netdisk_events = [e for e in events if any(e.startswith(s) for s in proto_suffixes)]
    facts = {
        "host_owned_prefixes": host,
        "kernel_owned_bridge_prefixes": kernel,
        "host_fireable_events": events,
        "protocol_namespaces": protos,
        "protocol_capabilities": _registry_protocol_capabilities(reg),
        "netdisk": {
            "namespaces": [p[:-1] for p in netdisk_prefixes],
            "permission_gated_events": netdisk_events,
            "schemas": [p.name for p in [SRC / d for d in _netdisk_schema_paths(reg)]
                        if p.is_file()],
        },
    }
    return facts


def kernel_mv3_permissions() -> dict:
    text = _read_source(MV3_PERMISSION_MAP)
    rows = {}
    for m in re.finditer(
            r'\{\s*"([^"]+)"\s*,\s*PermVerdict::(\w+)\s*,\s*(?:"([^"]*)"|nullptr)\s*\}', text):
        rows[m.group(1)] = {"verdict": m.group(2), "capability": m.group(3) or ""}
    if not rows:
        err("MV3 权限表解析结果为空 —— 解析器与源码格式漂移。")
    return rows


def kernel_perm_verdict_enum() -> dict:
    """解析 C++ enum class PermVerdict { kX = N, ... }; 返回 name → int。

    这是 native 侧权限 verdict 的权威数值表；Java 侧 PERM_* 必须与它 1:1 对齐
    （K-02，正是 K-01「native 7 / Java 8」混装错误的拦截点）。"""
    text = _strip_comments(_read_source(MV3_PERMISSION_MAP_H))
    m = re.search(r"enum\s+class\s+PermVerdict\s*\{(.*?)\};", text, re.S)
    if not m:
        err(f"{MV3_PERMISSION_MAP_H.as_posix()} 里找不到 enum class PermVerdict —— "
            f"解析器与源码已漂移。")
    out = {mm.group(1): int(mm.group(2))
           for mm in re.finditer(r"(k[A-Za-z]+)\s*=\s*(-?\d+)", m.group(1))}
    if not out:
        err(f"{MV3_PERMISSION_MAP_H.as_posix()} 的 PermVerdict 枚举解析不到任何成员。")
    return out


def kernel_java_guard_perm_constants() -> dict:
    """解析 ArupaMv3Guard.java 的 public static final int PERM_* = N; 返回 name → int。"""
    text = _read_source(JAVA_MV3_GUARD)
    out = {mm.group(1): int(mm.group(2)) for mm in re.finditer(
        r"public\s+static\s+final\s+int\s+(PERM_[A-Za-z_]+)\s*=\s*(-?\d+)\s*;",
        text)}
    if not out:
        err(f"{JAVA_MV3_GUARD.as_posix()} 里解析不到任何 PERM_* 常量 —— "
            f"解析器与源码已漂移。")
    return out


def kernel_java_guard_expect_perms() -> list:
    """解析 ArupaMv3Guard.selfTest() 里的 expectPerm("perm", PERM_X, "cap"/null)。

    返回 [(perm, verdict_const_name, want_cap_or_None)]；want_cap 为 None 表示 Java
    不约束能力（对应 Java 里传 null）。每条都必须在 native 权限表里逐条同源。"""
    text = _read_source(JAVA_MV3_GUARD)
    out = []
    for mm in re.finditer(
            r'expectPerm\(\s*"([^"]+)"\s*,\s*(PERM_[A-Za-z_]+)\s*,\s*'
            r'((?:null)|"[^"]*")\s*\)', text):
        cap = None if mm.group(3) == "null" else mm.group(3).strip('"')
        out.append((mm.group(1), mm.group(2), cap))
    if not out:
        err(f"{JAVA_MV3_GUARD.as_posix()} 的 selfTest() 里解析不到任何 expectPerm 调用 "
            f"—— 解析器与源码已漂移。")
    return out


def _check_perm_verdict_enum(cpp_enum: dict, java_consts: dict) -> list:
    """C++ PermVerdict 与 Java PERM_* 必须 1:1 数值对齐。

    两侧命名故意不同（kXxx vs PERM_Xxx），所以只比**数值集合**是否相等：任一侧
    独有数值或数值重复都意味着契约漂移，能直接拦下「native 7 / Java 8」类错误。"""
    fails = []
    cpp_vals, java_vals = set(cpp_enum.values()), set(java_consts.values())
    only_cpp = sorted(cpp_vals - java_vals)
    only_java = sorted(java_vals - cpp_vals)
    if only_cpp:
        fails.append("C++ PermVerdict 有 Java 没有的数值: " + str(only_cpp))
    if only_java:
        fails.append("Java PERM_* 有 C++ 没有的数值: " + str(only_java))
    for label, vals in (("C++", list(cpp_enum.values())),
                        ("Java", list(java_consts.values()))):
        dups = sorted({v for v in vals if vals.count(v) > 1})
        if dups:
            fails.append(f"{label} 侧 PermVerdict 数值重复（1:1 崩塌）: {dups}")
    return fails


def _check_perm_table_contract(cpp_perms: dict, java_expects: list,
                               cpp_enum: dict, java_consts: dict) -> list:
    """对每个 Java 校验的权限，比较 verdict 数值与 capability。

    这是 K-01 类错误的精确拦截点：native 权限表（kTable）与 Java expectPerm 期望
    必须逐条同源，verdict 数值不等或 capability 不等都拒绝出包。"""
    fails = []
    for perm, const_name, want_cap in java_expects:
        if const_name not in java_consts:
            fails.append(f"Java expectPerm({perm}) 用了未知常量 {const_name}（门面缺该 PERM_*？）")
            continue
        want_val = java_consts[const_name]
        row = cpp_perms.get(perm)
        if row is None:
            # 该权限在 C++ 权限表里没有显式行：它由 MapPermission 的逻辑分支判定
            # （vendor 前缀 → kVendorNamespace、未知 → kDenyUnknown）。Java 与 native
            # 调的是同一个 native 函数，不存在 K-01 类「手写表 vs 手写期望」漂移；
            # 这类权限由 native 自检的 ExpectPerm 统一覆盖，这里不误报。
            continue
        cpp_val = cpp_enum.get(row["verdict"])
        if cpp_val is None:
            fails.append(f"C++ 权限 {perm} 的 verdict {row['verdict']} 不在 PermVerdict 枚举里。")
            continue
        if want_val != cpp_val:
            fails.append(f"权限 {perm} verdict 不一致: Java={const_name}({want_val}) vs "
                         f"C++={row['verdict']}({cpp_val})。")
        if want_cap is not None and want_cap != row["capability"]:
            fails.append(f"权限 {perm} capability 不一致: Java 期望 '{want_cap}' vs "
                         f"C++ 实际 '{row['capability']}'。")
    return fails


def verify_permission_contract() -> dict:
    """K-02 跨语言权限契约门禁（出包前强制）：C++ PermVerdict ↔ Java PERM_*，
    C++ 权限表 ↔ Java expectPerm。任一条不一致就 err() 拒绝出包。

    与网盘无关，必须在 verify_capability_claims 的 netdisk 早退之前运行，
    否则老内核（无网盘）能绕过这道门禁。"""
    cpp_enum = kernel_perm_verdict_enum()
    java_consts = kernel_java_guard_perm_constants()
    java_expects = kernel_java_guard_expect_perms()
    cpp_perms = kernel_mv3_permissions()
    fails = _check_perm_verdict_enum(cpp_enum, java_consts)
    fails += _check_perm_table_contract(cpp_perms, java_expects, cpp_enum, java_consts)
    if fails:
        err("跨语言权限契约不一致（K-02；正是 K-01 的量产通道）：\n  · "
            + "\n  · ".join(fails))
    return {"enum_pairs": len(cpp_enum),
            "java_consts": len(java_consts),
            "java_checked_perms": len(java_expects)}


def verify_permission_contract_regression() -> None:
    """K-02 负向回归夹具（CI/沙箱可断言，不触发 sys.exit）。

    构造「Java 把 browsingData 期望成 8 而 native 仍是 7」的篡改场景，门禁必须报失败；
    同时断言当前正常源码不被该门禁误伤。这把「门禁形同虚设」本身变成可测失败。"""
    cpp_enum = kernel_perm_verdict_enum()
    java_consts = kernel_java_guard_perm_constants()
    cpp_perms = kernel_mv3_permissions()
    real = kernel_java_guard_expect_perms()
    # 篡改：browsingData 的 verdict 常量替换成 8（PERM_DEGRADED_UNAVAILABLE）。
    tampered = [(p, "PERM_DEGRADED_UNAVAILABLE" if p == "browsingData" else c, cap)
                for (p, c, cap) in real]
    if not _check_perm_table_contract(cpp_perms, tampered, cpp_enum, java_consts):
        err("K-02 负向回归夹具失效：把 browsingData 期望改成 8 后门禁没有报错 —— "
            "跨语言权限契约门禁形同虚设。")
    ok_fails = _check_perm_table_contract(cpp_perms, real, cpp_enum, java_consts)
    if ok_fails:
        err("K-02 负向回归夹具前提错误：当前正常源码就被门禁判失败，门禁可能误伤：\n  · "
            + "\n  · ".join(ok_fails))


# ── K-03/K-04 内核能力契约（单一来源派生 + 提交产物 + 同步门禁 + AAR 反向核对）──────
def _canonical_contract() -> dict:
    """从源码派生**语义契约**（不含 git 修订等每次提交都变的字段，保证 digest 稳定）。

    只纳入会被混装影响、且两侧（Java/native）都依赖的事实：PermVerdict 数值表、
    权限三元组、路由前缀、宿主可发事件、协议命名空间。"""
    reg = _read_registry()
    routing = reg["routing"]
    enum_ = kernel_perm_verdict_enum()
    perms = kernel_mv3_permissions()
    return {
        "schemaVersion": CONTRACT_SCHEMA_VERSION,
        "permVerdict": {k: enum_[k] for k in sorted(enum_)},
        "permissions": [
            {"name": p, "verdict": perms[p]["verdict"], "capability": perms[p]["capability"]}
            for p in sorted(perms)
        ],
        "routing": {
            "hostOwnedPrefixes": sorted(routing["host_owned_prefixes"]),
            "kernelOwnedBridgePrefixes": sorted(routing["kernel_owned_bridge_prefixes"]),
            "hostFireableEvents": sorted(routing["host_fireable_events"]),
        },
        "protocolNamespaces": sorted(_registry_protocol_namespaces(reg)),
    }


def _contract_payload(doc: dict) -> str:
    """规范化序列化：与 JSON 产物无关的稳定字节串（用于算 digest）。"""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_contract() -> dict:
    """语义契约 + contractDigest + contractEpoch（digest 覆盖规范化后的语义契约本身）。"""
    doc = _canonical_contract()
    digest = hashlib.sha256(_contract_payload(doc).encode("utf-8")).hexdigest()
    doc = dict(doc)
    doc["contractDigest"] = digest
    doc["contractEpoch"] = digest[:16]
    return doc


def _render_contract_json(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def _render_contract_header(doc: dict) -> str:
    return (
        "// 自动生成，请勿手改 —— 重跑: python3 scripts/kernel_capability.py contract --write\n"
        "// K-04：native 与 Java 各自嵌入同一份契约身份；启动时 O(1) 比对，不一致即\n"
        "// KERNEL_CONTRACT_MISMATCH（陈旧/混装 AAR 的物理拦截点）。\n"
        "#ifndef CHROME_BROWSER_ARUPA_ANDROID_MV3_ARUPA_KERNEL_CONTRACT_H_\n"
        "#define CHROME_BROWSER_ARUPA_ANDROID_MV3_ARUPA_KERNEL_CONTRACT_H_\n"
        "\n"
        "namespace arupa::mv3::contract {\n"
        f"inline constexpr int kSchemaVersion = {doc['schemaVersion']};\n"
        f"inline constexpr char kDigest[] = \"{doc['contractDigest']}\";\n"
        f"inline constexpr char kEpoch[] = \"{doc['contractEpoch']}\";\n"
        "}  // namespace arupa::mv3::contract\n"
        "\n"
        "#endif  // CHROME_BROWSER_ARUPA_ANDROID_MV3_ARUPA_KERNEL_CONTRACT_H_\n"
    )


def _render_contract_java(doc: dict) -> str:
    return (
        "package co.arupa.mv3;\n"
        "\n"
        "// 自动生成，请勿手改 —— 重跑: python3 scripts/kernel_capability.py contract --write\n"
        "// K-04：与 native 内嵌同一契约身份；ArupaMv3Guard.verifyContractIdentity() 启动时\n"
        "// 与 JNI 返回的 native digest 做 O(1) 比对（陈旧/混装 AAR 的物理拦截点）。\n"
        "public final class ArupaKernelContract {\n"
        "    private ArupaKernelContract() {}\n"
        "\n"
        f"    public static final int SCHEMA_VERSION = {doc['schemaVersion']};\n"
        f"    public static final String DIGEST = \"{doc['contractDigest']}\";\n"
        f"    public static final String EPOCH = \"{doc['contractEpoch']}\";\n"
        "}\n"
    )


def write_contract(repo_root: Path) -> list:
    """把三份契约产物写到源码树（单一来源派生的提交产物）。"""
    doc = build_contract()
    arts = [(CONTRACT_JSON, _render_contract_json(doc)),
            (CONTRACT_HEADER, _render_contract_header(doc)),
            (CONTRACT_JAVA, _render_contract_java(doc))]
    written = []
    for rel, text in arts:
        p = (repo_root / rel).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(p)
    log(f"  契约产物已写出: digest={doc['contractDigest'][:16]} epoch={doc['contractEpoch']} "
        f"schema=v{doc['schemaVersion']}")
    return written


def verify_contract_sync() -> dict:
    """K-04 同步门禁：提交的契约产物必须与当前源码派生的一致（否则漏跑 contract --write）。"""
    doc = build_contract()
    for rel, expected in ((CONTRACT_JSON, _render_contract_json(doc)),
                          (CONTRACT_HEADER, _render_contract_header(doc)),
                          (CONTRACT_JAVA, _render_contract_java(doc))):
        p = SRC / rel
        if not p.is_file():
            err(f"缺契约产物 {rel.as_posix()} —— 运行 "
                f"python3 scripts/kernel_capability.py contract --write 生成后再出包。")
        got = p.read_text(encoding="utf-8")
        if got != expected:
            err(f"契约产物与源码不同源: {rel.as_posix()} —— "
                f"权限表/枚举/路由变了却没重跑 contract --write。")
    # 顺带保证 AAR 内资产名与产物路径一致（打包反向核对认这个）。
    if CONTRACT_JSON.name != Path(CONTRACT_ASSET_NAME).name:
        err("契约 JSON 文件名与 AAR 资产名不一致 —— 打包反向核对会找错文件。")
    return {"digest": doc["contractDigest"], "epoch": doc["contractEpoch"],
            "schema_version": doc["schemaVersion"]}


def verify_aar_contract(aar_path: Path, expected: dict | None = None) -> dict:
    """K-03：从 AAR 内解包读取内嵌契约，与源码派生契约逐字段核对。

    只证明「这份 AAR 里真实带的契约」与当前源码同源，而不是只看工作区源码。
    SHA256 只证明文件后来没变，证明不了它自带哪份契约。"""
    import zipfile
    aar_path = Path(aar_path)
    if not aar_path.is_file():
        err(f"找不到 AAR: {aar_path}")
    want = expected or build_contract()
    with zipfile.ZipFile(aar_path) as z:
        names = z.namelist()
        if CONTRACT_ASSET_NAME not in names:
            err(f"{aar_path.name} 里没有内嵌契约 {CONTRACT_ASSET_NAME} —— "
                f"这份 AAR 无法证明与源码同源（拒绝交付）。")
        got = json.loads(z.read(CONTRACT_ASSET_NAME).decode("utf-8"))
    for key in ("schemaVersion", "contractDigest", "permVerdict",
                "permissions", "routing", "protocolNamespaces"):
        if got.get(key) != want.get(key):
            err(f"{aar_path.name} 内嵌契约与源码不同源: 字段 {key} 不一致 —— "
                f"陈旧/混装的 AAR，拒绝交付。")
    return {"aar": aar_path.name, "digest": got["contractDigest"],
            "schema_version": got["schemaVersion"]}


def verify_cross_abi_contract(aar_paths: list) -> dict:
    """K-05：多 ABI 的 AAR 必须内嵌同一份契约（digest/schema 一致，仅允许 native/ABI 不同）。"""
    import zipfile
    seen = {}
    for p in aar_paths:
        p = Path(p)
        with zipfile.ZipFile(p) as z:
            if CONTRACT_ASSET_NAME not in z.namelist():
                err(f"{p.name} 里没有内嵌契约 {CONTRACT_ASSET_NAME} —— 无法做跨 ABI 比对。")
            c = json.loads(z.read(CONTRACT_ASSET_NAME).decode("utf-8"))
        seen[p.name] = (c.get("contractDigest"), c.get("schemaVersion"))
    digests = {v[0] for v in seen.values()}
    schemas = {v[1] for v in seen.values()}
    if len(digests) > 1 or len(schemas) > 1:
        err("跨 ABI 契约不一致（不同 ABI 会得到不同能力行为）：\n  "
            + "\n  ".join(f"  {k}: digest={v[0][:16]} schema=v{v[1]}" for k, v in seen.items()))
    return {"count": len(seen), "digest": next(iter(digests))}


# ── K-06 结构化自检结果 ────────────────────────────────────────────────────────
def kernel_selftest_facts() -> dict:
    """结构化自检（K-06）的接线事实：native 产出结构化 JSON、结构体带 items、
    JNI 与 Java 门面都暴露 selfTestJson —— 缺一个，宿主就只能回去匹配自由文本。"""
    h = _strip_comments(_read_source(SELF_TEST_H))
    cc = _strip_comments(_read_source(SELF_TEST_SOURCE))
    jni = _strip_comments(_read_source(MV3_GUARD_JNI))
    java = _read_source(JAVA_MV3_GUARD)
    json_fn = ""
    m = re.search(r"std::string\s+SelfTestJson\(\)\s*\{(.*?)\n\}", cc, re.S)
    if m:
        json_fn = m.group(1)
    return {
        "struct_has_failure": "struct SelfTestFailure" in h,
        "struct_has_items": "std::vector<SelfTestFailure> items" in h,
        "records_structured_failure": "r.items.push_back" in cc,
        "json_fn_declared": "std::string SelfTestJson();" in h,
        "json_fn_defined": bool(json_fn),
        # 必备字段：schemaVersion / contractDigest / pass / fail / retryable / failures。
        "json_fields": all(f'"{k}"' in json_fn for k in
                           ("schemaVersion", "contractDigest", "pass", "fail",
                            "retryable", "failures")),
        "json_layer_classified": "permission-contract" in json_fn,
        "jni_export": "JNI_ArupaMv3Guard_SelfTestJson" in jni and
                      "arupa::mv3::SelfTestJson()" in jni,
        "java_facade": "public static String selfTestJson()" in java and
                       "selfTestJson();" in java and
                       "String selfTestJson();" in java,
    }


def verify_selftest_contract(facts: dict) -> int:
    """K-06 结构化自检的接线契约（native 结构体/JSON/JNI/Java 门面）逐条核。"""
    failures = {
        "struct_has_failure": "SelfTestResult 没有 SelfTestFailure 结构 —— 失败项没有结构化层。",
        "struct_has_items": "SelfTestResult 没有 items 列表 —— 结构化失败项无处存放。",
        "records_structured_failure": "Expect 没有把失败记为结构化项（r.items.push_back）—— "
                                      "依旧只能靠自由文本。",
        "json_fn_declared": "头文件里没有 SelfTestJson() 声明。",
        "json_fn_defined": "self_test.cc 里没有 SelfTestJson() 定义。",
        "json_fields": "SelfTestJson 缺必备字段（schemaVersion/contractDigest/pass/fail/"
                       "retryable/failures）—— 宿主拿不到稳定协议。",
        "json_layer_classified": "SelfTestJson 没有区分契约层失败（permission-contract）—— "
                                 "retryable 无法正确分类。",
        "jni_export": "JNI 没有导出 SelfTestJson（mv3_guard_jni.cc）。",
        "java_facade": "Java 门面没有 selfTestJson()（声明 + 调用 + Natives 接口）。",
    }
    bad = [n for n, ok in facts.items() if n in failures and not ok]
    if bad:
        err("K-06 结构化自检接线不成立：\n"
            + "\n".join(f"  · {failures[n]}" for n in bad))
    return len(failures)


def kernel_dispatched_events() -> list:
    """内核实际会分发的扩展事件（listExtListeners 判 honored 的依据）。"""
    text = _strip_comments(_read_source(LISTENER_REGISTRY))
    events = _quoted_strings(_array_body(text, "kDispatchedEvents", LISTENER_REGISTRY))
    _check_table("kDispatchedEvents", events)
    return events


def kernel_worker_window_facts() -> dict:
    """受控执行窗口的预算与结果码；另给 `window_facts`：键名与运行期 JSON 逐键同名。"""
    c = _require_consts(WORKER_WINDOW_HEADER,
                        tuple(WINDOW_CODE_ALIASES) +
                        ("kStartupBudgetMs", "kBusinessBudgetMaxMs", "kCommitReserveMs",
                         "kMaxLeaseMs", "kMaxItems", "kMaxLiveWindowsPerExtension"))
    return {
        "startup_budget_ms": c["kStartupBudgetMs"],
        "business_budget_max_ms": c["kBusinessBudgetMaxMs"],
        "commit_reserve_ms": c["kCommitReserveMs"],
        "max_lease_ms": c["kMaxLeaseMs"],
        "max_items": c["kMaxItems"],
        "max_live_windows_per_extension": c["kMaxLiveWindowsPerExtension"],
        "accepted_budget_range_ms": f"1..{c['kBusinessBudgetMaxMs']}",
        "boundary": f"{c['kBusinessBudgetMaxMs']} 允许 / {c['kBusinessBudgetMaxMs'] + 1} 拒绝",
        "result_codes": {n: c[n] for n in WINDOW_CODE_ALIASES},
        "window_facts": {
            "maxLeaseMs": c["kMaxLeaseMs"],
            "startupBudgetMs": c["kStartupBudgetMs"],
            "businessBudgetMaxMs": c["kBusinessBudgetMaxMs"],
            "commitReserveMs": c["kCommitReserveMs"],
            "maxItems": c["kMaxItems"],
            "maxLivePerExtension": c["kMaxLiveWindowsPerExtension"],
        },
    }


def kernel_wake_facts() -> dict:
    c = _require_consts(WAKE_OUTBOX_HEADER, tuple(WAKE_CODE_ALIASES))
    return {"error_codes": {n: c[n] for n in WAKE_CODE_ALIASES},
            "wake_id_contract": ">0 = wakeId；<0 = 错误码"}


def kernel_runtime_window_keys() -> list:
    """解析 outbox 的 CapabilitiesJson 真正写出去的窗口键名。"""
    text = _strip_comments(_read_source(WAKE_OUTBOX_SOURCE))
    body = text[text.find("CapabilitiesJson"):] if "CapabilitiesJson" in text else ""
    if not body:
        err(f"{WAKE_OUTBOX_SOURCE.as_posix()} 里找不到 CapabilitiesJson —— "
            f"运行期能力 JSON 的窗口段解析不了，清单不能凭空声明键名。")
    return sorted(set(re.findall(r'window\.Set\(\s*"([^"]+)"', body)))


def kernel_java_facade_facts() -> dict:
    text = _read_source(JAVA_BRIDGE_FACADE)
    codes = {m.group(1): int(m.group(2))
             for m in re.finditer(
                 r"public\s+static\s+final\s+(?:int|long)\s+((?:WINDOW|WAKE)_\w+)\s*=\s*(-?\d+)L?\s*;",
                 text)}
    return {"codes": codes,
            "runtime_entrypoints": [m for m in RUNTIME_FACT_ENTRYPOINTS
                                    if re.search(r"\b" + m + r"\s*\(", text)]}


def kernel_netdisk_schema_facts() -> dict:
    out = {}
    for rel in _netdisk_schema_paths(_read_registry()):
        p = SRC / rel
        if not p.is_file():
            continue
        for ns in json.loads(p.read_text(encoding="utf-8")):
            out[ns["namespace"]] = {
                "schema": rel.name,
                "functions": [f["name"] for f in ns.get("functions", [])],
                "events": [e["name"] for e in ns.get("events", [])],
            }
    return out


def kernel_dnr_facts() -> dict:
    """declarativeNetRequest 面的事实：schema 声明的方法、内核自认的支持表、
    分发路由、错误码、两处极限值（内核常量 + schema properties）。"""
    schema = json.loads((SRC / DNR_SCHEMA).read_text(encoding="utf-8"))
    declared = sorted(fn["name"] for ns in schema for fn in ns.get("functions", []))
    header = _strip_comments(_read_source(DNR_RULES_HEADER))
    supported = sorted(_quoted_strings(
        _array_body(header, "kSupportedDnrMethods", DNR_RULES_HEADER)))
    _check_table("kSupportedDnrMethods", supported)
    limits = _constexpr_ints(DNR_RULES_HEADER)
    schema_limits = {}
    for ns in schema:
        for key, prop in (ns.get("properties") or {}).items():
            schema_limits[key] = prop.get("value")
    host = _read_source(EXT_API_HOST)
    codes = dict(re.findall(r'constexpr char (kDnrErr\w+)\[\] = "(E_DNR_\w+)"', host))
    if not codes:
        err(f"{EXT_API_HOST.name} 里解析不到任何 E_DNR_* 错误码 —— "
            f"错误码又回到自由文本了？(S-03)")
    return {
        "supported_methods": supported,
        "schema_methods": declared,
        "limits": limits,
        "schema_limits": schema_limits,
        "error_codes": {code: name for name, code in codes.items()},
    }


# ── 门禁 ────────────────────────────────────────────────────────────────────
def verify_consumer_keep_rules() -> int:
    """交付件混淆契约：必需 keep 规则必须显式写在内核的 proguard.flags 里。"""
    merged = []
    for rel in sorted(SRC.glob("chrome/browser/arupa_android/**/proguard.flags")):
        for line in rel.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0].strip()      # 与 App 侧同口径
            if code:
                merged.append(code)
    if not merged:
        err("找不到任何 proguard.flags —— 交付件的混淆契约无法核对（别静默放过）")
    text = " ".join(" ".join(merged).split())
    missing = [r for r in REQUIRED_CONSUMER_KEEP if r not in text]
    if missing:
        err("交付件混淆契约缺规则（App 的 verifyMv3KernelDelivery 会拒绝构建）：\n  "
            + "\n  ".join(missing))
    return len(REQUIRED_CONSUMER_KEEP)


def verify_no_fake_keepalive() -> None:
    """R3「不假保活」：SW 生命周期只能由受控窗口的在持引用驱动。

    旧实现是"Worker 一起来就 kDoesNotTimeout 常驻锁" → SW 永不被 idle 回收，
    且"窗口可用"与真实预算脱钩。这里把口径钉死，防回退。
    """
    text = _strip_comments(_read_source(SW_HOST_SOURCE))
    bodies = _cpp_function_bodies(text)
    if "AcquireWindowKeepAlive" not in bodies or "ReleaseWindowKeepAlive" not in bodies:
        err(f"{SW_HOST_SOURCE.name} 里找不到保活获取/归还函数 —— 实现变了，先修本门禁"
            f"（别让检查静默失效）。")
    offenders = [name for name, body in bodies.items()
                 if "kDoesNotTimeout" in body and name != "AcquireWindowKeepAlive"]
    if offenders:
        err("又出现常驻锁（R3 假保活）：kDoesNotTimeout 出现在 " + " / ".join(sorted(offenders))
            + "\n  生命周期延长只能来自受控窗口的在持引用（AcquireWindowKeepAlive）。")
    if "StartingExternalRequest" in bodies.get("OnWorkerInfo", ""):
        err("Worker 启动回调 OnWorkerInfo 里又出现 StartingExternalRequest —— "
            "启动即常驻 = 假保活。")
    if "FinishedExternalRequest" not in bodies.get("ReleaseWindowKeepAlive", ""):
        err("ReleaseWindowKeepAlive 没有 FinishedExternalRequest —— 锁借出去还不回来。")


#: schema 的 properties 名 ↔ 内核常量名：同一个数字写两处，必须相等
#: （schema 自己就在注释里写着"改了一处必须改另一处"）。
DNR_LIMIT_PARITY = {
    "kMaxDynamicRules": "MAX_NUMBER_OF_DYNAMIC_AND_SESSION_RULES",
    "kMaxRegexRules": "MAX_NUMBER_OF_REGEX_RULES",
    "kGuaranteedMinimumStaticRules": "GUARANTEED_MINIMUM_STATIC_RULES",
    "kMaxEnabledStaticRulesets": "MAX_NUMBER_OF_ENABLED_STATIC_RULESETS",
    "kMaxStaticRulesets": "MAX_NUMBER_OF_STATIC_RULESETS",
}


def verify_dnr_surface(facts: dict) -> int:
    """DNR：schema ↔ 内核支持表 ↔ 分发路由 三处同源（S-04 的正面收口）。

    为什么单设这一道：2026-10-13 实测发现 getEnabledRulesets /
    updateEnabledRulesets 业务实现齐全，但分发只接了 getDynamicRules /
    updateDynamicRules，运行期一律回 "not supported in this browser" —— 静态规则集
    启停整条主链因此不可达。这种「声明有、实现有、**分发没有**」的漏接，任何断言都
    发现不了，只能把三处摆在一起比。
    """
    host = _read_source(EXT_API_HOST)
    supported, declared = facts["supported_methods"], facts["schema_methods"]
    if supported != declared:
        err("DNR 声明与实现不同源：\n"
            f"  schema 有、内核支持表没有: "
            f"{[m for m in declared if m not in supported] or '无'}\n"
            f"  内核支持表有、schema 没有: "
            f"{[m for m in supported if m not in declared] or '无'}\n"
            f"  (schema={DNR_SCHEMA.as_posix()}；支持表="
            f"{DNR_RULES_HEADER.as_posix()}::kSupportedDnrMethods)")
    if not re.search(r'HasApiPrefix\(\s*name\s*,\s*"declarativeNetRequest\."\s*\)', host):
        err(f"{EXT_API_HOST.name} 没有按命名空间整体路由 declarativeNetRequest.* —— "
            "少接一个方法就会静默掉到通用兜底 \"not supported in this browser\""
            "（S-04 的原样）：必须像其他内核命名空间一样整体移交 HandleKernelOwnedDnr。")
    no_branch = [m for m in supported if f'"declarativeNetRequest.{m}"' not in host]
    if no_branch:
        err(f"{EXT_API_HOST.name} 里没有这些方法的分支: {no_branch} —— "
            "路由会把它们交给 HandleKernelOwnedDnr，但那里没有对应处理。")
    # 错误码：声明了却从没抛出 = 插件永远看不到（S-03 的一半）。
    for code, name in facts["error_codes"].items():
        if len(re.findall(rf"\b{re.escape(name)}\b", host)) < 2:
            err(f"错误码 {code}（常量 {name}）声明了却从没被抛出 —— "
                "插件按码分支时永远等不到它（声明与实现不同源）。")
    # 错误回执必须结构化：每个错误返回（`Run(std::string(), …)`）都要带 DnrError。
    handler = _cpp_function_bodies(_strip_comments(host)).get("HandleKernelOwnedDnr")
    if not handler:
        err(f"{EXT_API_HOST.name} 里找不到 HandleKernelOwnedDnr —— 实现改名了，先修门禁。")
    bare = len(re.findall(r"Run\(\s*std::string\(\)\s*,", handler))
    coded = handler.count("DnrError(")
    if coded < bare:
        err(f"HandleKernelOwnedDnr 里有 {bare} 个错误返回，但只有 {coded} 处用了 "
            "DnrError() —— 自由文本错误串会把插件逼回「匹配中文」那条路（S-03）。")
    for const, prop in DNR_LIMIT_PARITY.items():
        want = facts["schema_limits"].get(prop)
        got = facts["limits"].get(const)
        if want is None:
            err(f"schema 里找不到 {prop} —— 门禁与 schema 已漂移，先修门禁。")
        if got != want:
            err(f"{prop} 与内核常量不同源：schema={want} vs {const}={got} "
                f"（{DNR_RULES_HEADER.name} 与 {DNR_SCHEMA.name} 必须同改）。")
    return len(supported)


def kernel_reinject_facts() -> dict:
    """R-03 补注入面的源码事实。

    这一面最容易被"看起来接了"骗过去：写了模块却没人调、调了却落在错误分支（宿主每次
    重复调启用都会再注入一遍）、代次判活被删（停用后迟到的回执又把旧层写回页面）、
    编译目标没接线或成环。逐条当事实核，不靠函数名存在与否。
    """
    bridge = _strip_comments(_read_source(LOADER_BRIDGE_SOURCE))
    bodies = _cpp_function_bodies(bridge)
    # 启用/停用的状态跳变逻辑落在 UI 线程入口 SetExtensionEnabledOnUi（SetExtensionEnabled
    # 只做线程跳转），两处拼接起来核，避免把逻辑挪一层就误报"没接"。
    enable = (bodies.get("SetExtensionEnabled", "") + "\n" +
              bodies.get("SetExtensionEnabledOnUi", ""))
    purge = bodies.get("PurgeExtensionData", "")
    source = _strip_comments(_read_source(REINJECT_SOURCE))
    header = _strip_comments(_read_source(REINJECT_HEADER))
    # 诚实边界说明写在注释里，必须看**未剥注释**的原文。
    header_raw = _read_source(REINJECT_HEADER)
    build = _read_source(PLUGIN_BUILD_GN)

    def gn_target_body(name: str) -> str:
        match = re.search(rf'source_set\("{re.escape(name)}"\)\s*\{{(.*?)\n  \}}',
                          build, re.S)
        return match.group(1) if match else ""

    target_body = gn_target_body("arupa_content_script_reinject")
    bridge_body = gn_target_body("arupa_extension_loader_bridge")
    reinject_call = enable.find("ReinjectContentScriptsIntoExistingDocuments(")
    bump_call = enable.find("BumpActionForExtension(")
    return {
        # 启用路径真的补注入，且落在 state_changed 分支（BumpActionForExtension 只在
        # 那个分支里调；补注入在它之后 = 同分支），避免"宿主重复调启用 → 双重注入"。
        "enable_calls_reinject": reinject_call >= 0,
        "reinject_after_state_changed": 0 <= bump_call < reinject_call,
        # 停用与卸载都要作废代次，否则迟到回执会在停用之后把旧层写回页面。
        "disable_invalidates": "InvalidateContentScriptGeneration(" in enable,
        "uninstall_invalidates": "InvalidateContentScriptGeneration(" in purge,
        # 代次判活 + 迟到回执计数 + 机读回执。判活这一条必须看到"拿本任务的代次去比"
        # 的那行比较：只留一个同名的恒真函数（GenerationStillCurrent 里 return true）
        # 等于判活被拆掉了，光看函数名是看不出来的。
        "generation_guard": (
            "GenerationStillCurrent" in source and
            bool(re.search(r"GenerationOf\([^;]*\)\s*==\s*generation_", source))),
        "stale_counter": "stale_receipts_dropped" in source,
        "receipt_json": ("LastReinjectReportJson" in source and
                         "LastReinjectReportJson" in header),
        "receipt_verdict": '"verdict"' in source,
        # 逐帧判权而不是"投了就完事"。
        "per_frame_permission": ("CanAccessPage(" in source and
                                 "frames_denied" in source),
        "registered_page_enumeration": "ForEachRegisteredPage(" in source,
        # Android 侧注入授权登记只有写入方（桌面端读取点在 arupa_user_script_dispatch.cc）。
        # 这条说明是"诚实边界"的一部分，不能被悄悄删掉后当成"授权已闭环"。
        "grants_reader_gap_documented": "只有写入方" in header_raw,
        # 编译接线：新目标存在、loader bridge 依赖它、且新目标**不**反向依赖（成环会
        # 直接编译失败，但门禁要在出包前就说清，而不是等 ninja 报错）。
        "compiled": bool(target_body),
        "loader_bridge_depends_on_it": (
            "arupa_content_script_reinject" in bridge_body),
        "no_dependency_cycle": (
            "arupa_extension_loader_bridge" not in target_body),
    }


def verify_reinjection_contract(facts: dict) -> int:
    """R-03：补注入的接线契约（启用/停用/代次/回执/编译）逐条核。"""
    failures = {
        "enable_calls_reinject":
            "SetExtensionEnabled 的启用路径没有调用 "
            "ReinjectContentScriptsIntoExistingDocuments —— 停用再启用后既有文档"
            "依旧不会恢复（R-03 原样复现）。",
        "reinject_after_state_changed":
            "补注入没有落在 state_changed 分支里（应在 BumpActionForExtension 之后）"
            " —— 宿主每次重复调启用都会把内容脚本再注入一遍：双重注入。",
        "disable_invalidates":
            "停用路径没有作废内容脚本代次 —— 停用那一刻还在途的补注入回执会在停用"
            "之后把旧阅读层写回页面（清单：卸载中的迟到回包不能复活旧会话）。",
        "uninstall_invalidates":
            "PurgeExtensionData 没有作废内容脚本代次 —— 卸载后抵达的回执仍会改页面。",
        "generation_guard":
            "补注入回执没有代次判活 —— 上一代的回执会被当成有效回执。",
        "stale_counter":
            "没有统计被丢弃的迟到回执（stale_receipts_dropped）—— 丢弃是静默的，"
            "出问题无从判定。",
        "receipt_json":
            "缺少机读回执（LastReinjectReportJson / 头文件声明）—— 宿主与设备用例"
            "拿不到同一份事实。",
        "receipt_verdict":
            "回执里没有 verdict 字面量 —— 宿主/门禁只能靠数字猜「恢复没恢复」。",
        "per_frame_permission":
            "补注入没有逐帧判权（CanAccessPage / frames_denied）—— 被拒的帧会被"
            "静默跳过，正是「说恢复了、其实没恢复」。",
        "registered_page_enumeration":
            "补注入没有枚举既有页面（ForEachRegisteredPage）—— 看不到已打开的页，"
            "补注入无从谈起。",
        "grants_reader_gap_documented":
            "R-03 模块头里缺「授权登记只有写入方」这条诚实边界说明 —— 删掉它等于"
            "把未闭环的授权链当成已闭环。",
        "compiled":
            "plugin/BUILD.gn 里没有 arupa_content_script_reinject 编译目标。",
        "loader_bridge_depends_on_it":
            "arupa_extension_loader_bridge 没有依赖 arupa_content_script_reinject ——"
            "改完编不过，或（更糟）链接到了别处的旧实现。",
        "no_dependency_cycle":
            "arupa_content_script_reinject 反向依赖 arupa_extension_loader_bridge ——"
            "GN 依赖成环（调用方才是依赖方）。",
    }
    bad = [name for name, ok in facts.items() if name in failures and not ok]
    if bad:
        err("R-03 补注入接线不成立：\n"
            + "\n".join(f"  · {failures[name]}" for name in bad))
    return len(failures)


def kernel_storage_facts() -> dict:
    """chrome.storage.local / session / onChanged 的接线事实。

    这一组的教训：三个区原先共用一套"落盘 + 8KB/512 条配额"，于是 storage.session
    **会落盘**（Chrome 语义是重启即空）；超配额只 LOG 不回报，host 照"写前比较"派发
    onChanged、回调一律报成功；managed 可写；闹钟还把内核内部键写进 storage.local。
    这四件事任何断言都发现不了，只能逐条当事实核。
    """
    storage_cc = _strip_comments(_read_source(PLUGIN_STORAGE_CC))
    storage_h = _strip_comments(_read_source(PLUGIN_STORAGE_H))
    host = _strip_comments(_read_source(STORAGE_HOST))
    mojom = _read_source(STORAGE_MOJOM)
    ipc = _strip_comments(_read_source(STORAGE_IPC))
    alarms_raw = _read_source(ALARMS_SOURCE)

    return {
        # 区语义：session 只活在内存（不落盘、不从盘恢复）。
        "session_area_enum": "kSession" in storage_h and "StorageAreaFor" in storage_h,
        "session_not_persisted": storage_cc.count(
            "if (!AreaPersists(StorageAreaFor(pid))) {") >= 3,
        "session_quota": "kStorageQuotaBytesSession" in storage_h,
        # Chrome 口径：单条/条数上限只属于 sync；local/session 只有总量上限。
        "chrome_area_quotas": storage_cc.count(
            "return area == StorageArea::kSync;") >= 2,
        # 内核自用区不吃扩展配额（DNR 规则集可达数 MB，被配额吃掉 = 功能丢失）。
        "internal_area_unlimited": "return kInternalQuotaUnlimited;" in storage_cc,
        # 写入结果一路回传：后端 → host → mojom → IPC 回执。
        "write_result_enum": "StorageWriteResult" in storage_h,
        "write_result_message": "StorageWriteResultMessage" in storage_h,
        "mojom_error_channel": mojom.count("=> (string error)") >= 3,
        "ipc_error_reply": "OnStorageDone(int request_id," in ipc,
        # 写入已改为事务化批量提交（SetValuesAsync / RemoveValuesAsync /
        # ClearAsync），host 不再逐键同步写：判定与回执一律由提交结果
        # StorageCommitResult{status, changes} 派生。门禁跟着认新接口，
        # 否则会把"换了写法"误报成"没接"（旧字符串已不存在于源码）。
        "host_checks_result": (
            host.count("result.status == mv3::StorageWriteResult::kOk") >= 2 and
            host.count("mv3::StorageWriteResultMessage(result.status)") >= 2),
        # newValue 必须取自**落盘后**的新状态（after），且落盘失败要把 changes
        # 清空 —— 否则 onChanged 的 newValue 可能是根本没存进去的值。
        "host_reads_back_written": (
            "const base::Value* new_value = after.Find(key);" in storage_cc and
            storage_cc.count("result.changes.clear();") >= 2),
        # onChanged 只对真实写入派发（旧实现"写前比较"先算 changes 再统一写）。
        "changed_after_real_write": (
            host.count("DispatchStorageChanged(id, ChangesFromCommit(result))") >= 2),
        "no_prewrite_compare": "if (before == value_json) {" not in host,
        "remove_uses_result": (
            "PluginStorage::Get().RemoveValuesAsync(" in host and
            "!result.changes.empty()" in host),
        # managed 只读（Chrome：由策略下发）—— Set/Remove/Clear 三个写入口都要护住。
        "managed_readonly": (host.count("kReadOnlyArea") >= 3 and
                             host.count("mv3::StorageArea::kManaged") >= 3),
        # 内核内部键不进扩展可见区：闹钟必须落在私有区，不能再用 /local。
        "alarms_private_area": 'kAlarmsStorageArea[] = "__arupa_internal"' in alarms_raw,
        "alarms_not_in_local": 'extension_id + "/local"' not in alarms_raw,
    }


def verify_storage_contract(facts: dict) -> int:
    """chrome.storage 的接线契约（session 内存态 / 配额回报 / onChanged 真实性 / 内部键隔离）。"""
    failures = {
        "session_area_enum": "plugin_storage.h 没有 StorageArea / StorageAreaFor —— "
                             "storage.session 与 local 又共用一套语义了。",
        "session_not_persisted": "session 区没有做到「不落盘、不从盘恢复」 —— 重启后 "
                                 "session 数据还在（Chrome 语义是重启即空）。",
        "session_quota": "缺 kStorageQuotaBytesSession（session 区总量上限）。",
        "chrome_area_quotas": "单条/条数上限没有限定在 sync 上 —— storage.local 里放一个"
                              "大对象又会被静默拒写。",
        "internal_area_unlimited": "内核自用区仍吃扩展配额 —— DNR 规则集（可达数 MB）会被"
                                   "静默丢弃：那不是配额保护，是功能丢失。",
        "write_result_enum": "SetValue 没有写入结果（StorageWriteResult）—— 超配额只能静默。",
        "write_result_message": "缺 StorageWriteResultMessage —— 错误文案会各写一份。",
        "mojom_error_channel": "mojom 的 Set/Remove/Clear 没有 error 回传通道 —— "
                               "写失败根本没法告诉扩展。",
        "ipc_error_reply": "IPC 回执不带 error —— 错误到不了 JS（runtime.lastError）。",
        "host_checks_result": "host 写入没有按提交结果 StorageCommitResult 判定回执"
                              " —— Set/Remove 都必须先看 result.status。",
        "host_reads_back_written": "没有「写后回读」—— onChanged 的 newValue 可能是根本"
                                   "没存进去的值（changes 必须取自落盘后的新状态，"
                                   "落盘失败要清空）。",
        "changed_after_real_write": "onChanged 没有走真实写入路径 —— 必须由提交结果的 "
                                    "result.changes 派生，而不是写前比较。",
        "no_prewrite_compare": "又回到「写前比较」：被拒的键也会派发 onChanged。",
        "remove_uses_result": "host Remove 没有按提交结果（status/changes）判断变更。",
        "managed_readonly": "managed 区没有只读保护（Chrome：由策略下发）。",
        "alarms_private_area": "闹钟没有显式落在内核私有区。",
        "alarms_not_in_local": "闹钟又写回 storage.local 了 —— chrome.storage.local.get() "
                               "会看到内核内部键，还挤占扩展配额。",
    }
    bad = [n for n, ok in facts.items() if n in failures and not ok]
    if bad:
        err("chrome.storage 接线不成立：\n"
            + "\n".join(f"  · {failures[n]}" for n in bad))
    return len(failures)


def kernel_sw_facts() -> dict:
    """Service worker 的语义/自愈事实（onStartup 每会话一次、注册失败有界重试）。"""
    sw_host = _strip_comments(_read_source(SW_HOST_SOURCE))
    sw_service = _strip_comments(_read_source(SW_SERVICE_HOST_SOURCE))
    return {
        # runtime.onStartup：Chrome 语义是每个**浏览器会话**一次，不是每次 SW 启动都发
        # （"每次启动都发"会让扩展把 idle 回收后的重启误判成浏览器启动）。
        "onstartup_once_per_session": (
            "bool MarkStartupReported(" in sw_service and
            "else if (MarkStartupReported(extension_id))" in sw_service),
        # 注册失败要有界自愈：慢设备/冷启动首次失败很常见，只打日志会让扩展在本进程内
        # 一直停在"未注册"，宿主随后按启用失败回滚停用。
        "register_retry_bounded": (
            "kRegisterRetryMax" in sw_host and
            "if (attempt < kRegisterRetryMax)" in sw_host),
        # 两处都要收回计数：注册成功归零 + 重试用尽后放弃（只查"存在"区分不了这两条路，
        # 因为它们是同一句代码；少任何一处，偶发失败会被跨轮累积成永久放弃）。
        "register_retry_reset_on_success": sw_host.count(
            "RegisterRetries().erase(id);") >= 2,
        "register_retry_scheduled": "ScheduleRegisterRetry(id, this_attempt);" in sw_host,
    }


def verify_sw_contract(facts: dict) -> int:
    failures = {
        "onstartup_once_per_session":
            "runtime.onStartup 没有按「每会话一次」去重 —— 每次 SW 启动都会发，扩展会把"
            "idle 回收后的重启误判成浏览器启动。",
        "register_retry_bounded":
            "SW 注册失败没有有界重试 —— 慢设备首次失败后该扩展会一直停在未注册，宿主按"
            "启用失败回滚停用。",
        "register_retry_reset_on_success":
            "注册成功后没有把重试计数归零 —— 计数会跨轮累积，最终把偶发失败变成永久放弃。",
        "register_retry_scheduled":
            "注册失败没有真的安排重试（只打了日志）。",
    }
    bad = [n for n, ok in facts.items() if n in failures and not ok]
    if bad:
        err("Service worker 接线不成立：\n"
            + "\n".join(f"  · {failures[n]}" for n in bad))
    return len(failures)


def verify_capability_claims(routing: dict, permissions: dict, schemas: dict,
                             window: dict, wakes: dict, facade: dict) -> dict:
    """声明与实现同源门禁（**双向**）—— 回应 N-C08 / K-A09。

      · 声明有（清单/schema）→ 实现必须有（路由/事件/分发表/常量/门面）
      · 实现有（内核表）      → 声明必须有（schema / 事件白名单）
    任一条不成立就 err() 直接拒绝出包，绝不写一份"看起来通过"的清单。
    """
    # declarativeNetRequest 面与网盘无关，先核 —— 老内核没有网盘面也不能跳过它。
    dnr = kernel_dnr_facts()
    verify_dnr_surface(dnr)
    # R-03 补注入面同理：与网盘无关，不能被下面的早退跳过。
    reinject = kernel_reinject_facts()
    verify_reinjection_contract(reinject)
    # chrome.storage.local/session/onChanged 同理。
    storage = kernel_storage_facts()
    verify_storage_contract(storage)
    # Service worker：onStartup 每会话一次 + 注册失败有界自愈。
    sw = kernel_sw_facts()
    verify_sw_contract(sw)
    # K-06 结构化自检结果（与网盘无关，先于早退）。
    selftest = kernel_selftest_facts()
    verify_selftest_contract(selftest)
    # K-02 跨语言权限契约门禁（与网盘无关，必须先于下面的早退运行，否则老内核绕过）。
    perm_stats = verify_permission_contract()
    verify_permission_contract_regression()
    # K-04 契约产物同步门禁：提交的 C++/Java/JSON 必须与源码派生的一致。
    contract_stats = verify_contract_sync()
    if not routing["netdisk"]["namespaces"]:
        return {}                      # 老内核没有网盘面，不在此处拦
    protos = routing["protocol_namespaces"]
    caps = routing["protocol_capabilities"]
    missing = [d for d in _netdisk_schema_paths(_read_registry())
               if not (SRC / d).is_file()]
    if missing:
        err("内核接了网盘路由，但缺 schema（声明与实现不同源）：\n  "
            + "\n  ".join(d.as_posix() for d in missing))
    # 每条协议插件命名空间都必须有对应权限映射，且能力映射与登记表申明的 capability 一致。
    for perm in protos:
        row = permissions.get(perm)
        if row is None:
            err(f"内核接了网盘路由，但 MV3 权限表里没有 {perm}。")
        want_cap = caps[perm]
        if row["capability"] != want_cap:
            err(f"{perm} 的能力映射应为 {want_cap}，实际 {row['capability']!r}。")
    # 别名轨必须成对（每条命名空间都在宿主路由里）。
    for ns in protos:
        if f"{ns}." not in routing["host_owned_prefixes"]:
            err(f"网盘别名不完整：路由表缺 {ns}.（另一条在、这条不在，两条写法行为会不一致）。")
    # schema ↔ 路由。
    for ns, fact in schemas.items():
        if f"{ns}." not in routing["host_owned_prefixes"]:
            err(f"schema 声明了命名空间 {ns}，但路由表没有 {ns}. —— 能声明却调不通。")
        for fn in fact["functions"]:
            qualified = f"{ns}.{fn}"
            if not _has_prefix(routing["host_owned_prefixes"], qualified) and \
               not _has_prefix(routing["kernel_owned_bridge_prefixes"], qualified):
                err(f"schema 声明了 {qualified}，但内核既不等价转宿主也不桥接 —— 声明与路由不同源。")
        for ev in fact["events"]:
            qualified = f"{ns}.{ev}"
            if qualified not in routing["host_fireable_events"]:
                err(f"schema 声明了事件 {qualified}，但宿主事件白名单没有它 —— 声明了却发不出去。")
    for ev in routing["netdisk"]["permission_gated_events"]:
        if not any(ev.startswith(f"{ns}.") for ns in schemas):
            err(f"宿主事件白名单有 {ev}，但没有任何 schema 声明它 —— 实现有、声明没有。")
    # 事件 ↔ 内核分发表（双向）。
    dispatched = kernel_dispatched_events()
    for ev in routing["netdisk"]["permission_gated_events"]:
        if ev not in dispatched:
            err(f"事件 {ev} 可被宿主投递，但 {LISTENER_REGISTRY.name} 的 kDispatchedEvents "
                f"没有它 —— listExtListeners 会把它判成 honored=no。")
    for ev in [e for e in dispatched if any(e.startswith(ns + ".") for ns in protos)]:
        if ev not in routing["host_fireable_events"]:
            err(f"内核分发表有 {ev}，但宿主事件白名单没有它 —— 判定说会分发、实际投不进来。")
    # 窗口预算自洽 + v2.4 契约。
    if window["startup_budget_ms"] + window["business_budget_max_ms"] + \
            window["commit_reserve_ms"] != window["max_lease_ms"]:
        err(f"窗口预算不自洽：启动 {window['startup_budget_ms']} + 业务 "
            f"{window['business_budget_max_ms']} + 提交 {window['commit_reserve_ms']} "
            f"!= 最长租约 {window['max_lease_ms']}。")
    for key, want, label in (("business_budget_max_ms", CONTRACT_BUSINESS_BUDGET_MS, "业务窗口"),
                             ("max_lease_ms", CONTRACT_MAX_LEASE_MS, "最长租约"),
                             ("max_items", CONTRACT_MAX_ITEMS, "单次租约项数")):
        if window[key] != want:
            err(f"{label}偏离 v2.4 契约：内核 {window[key]} != 契约 {want}"
                f"（要改就连同 App NetdiskProductionWorkerWindows 与 spec 同批改）。")
    # 清单 window_facts ↔ 运行期 JSON 键名。
    declared_keys = sorted(window["window_facts"])
    runtime_keys = kernel_runtime_window_keys()
    if declared_keys != runtime_keys:
        err(f"窗口事实键名不一致：清单 window_facts={declared_keys} vs "
            f"运行期 {WAKE_OUTBOX_SOURCE.name} 的 window={runtime_keys}"
            f"（宿主按键核对，键名必须完全相同）")
    rc = window["result_codes"]
    if rc["kOk"] != 0 or len(set(rc.values())) != len(rc) or any(v < 0 for v in rc.values()):
        err(f"窗口结果码必须从 0 起互不相同且非负：{rc}")
    wake_codes = list(wakes["error_codes"].values())
    if any(c >= 0 for c in wake_codes) or len(set(wake_codes)) != len(wake_codes):
        err(f"唤醒错误码必须全部 < 0 且互不相同（>0 是 wakeId）：{wakes['error_codes']}")
    # 跨语言码 1:1。
    pairs = [(k, v, window["result_codes"][k]) for k, v in WINDOW_CODE_ALIASES.items()]
    pairs += [(k, v, wakes["error_codes"][k]) for k, v in WAKE_CODE_ALIASES.items()]
    for cpp_name, java_name, want in pairs:
        got = facade["codes"].get(java_name)
        if got is None:
            err(f"Java 门面缺常量 {java_name}（对应 C++ {cpp_name}）—— 宿主无法按码分支。")
        if got != want:
            err(f"跨语言码不一致：C++ {cpp_name}={want} vs Java {java_name}={got}。")
    missing_entries = [m for m in RUNTIME_FACT_ENTRYPOINTS if m not in facade["runtime_entrypoints"]]
    if missing_entries:
        err("清单声称可查运行期事实，但 Java 门面缺入口：" + " / ".join(missing_entries))
    keep_count = verify_consumer_keep_rules()
    verify_no_fake_keepalive()
    log("  能力同源门禁通过: 命名空间 " + " / ".join(sorted(schemas)) +
        " + schema " + " / ".join(routing["netdisk"]["schemas"]) +
        f" + 事件 {len(routing['netdisk']['permission_gated_events'])} 条(可发且会分发)" +
        f" + 别名成对 + 窗口 {window['max_lease_ms']}ms/业务 {window['business_budget_max_ms']}ms"
        f"/maxItems {window['max_items']}（键名与运行期一致 {len(declared_keys)} 个）"
        f" + 跨语言码 {len(pairs)} 条 + 运行期入口 {len(facade['runtime_entrypoints'])} 个"
        f" + DNR {len(dnr['supported_methods'])} 方法(schema↔支持表↔分发，"
        f"错误码 {len(dnr['error_codes'])} 个全部可达)"
        f" + R-03 补注入({len(kernel_reinject_facts())} 项接线契约全成立："
        f"启用跳变才补 / 代次判活 / 逐帧判权 / 机读回执 / 编译不成环)"
        f" + chrome.storage({len(kernel_storage_facts())} 项接线契约全成立："
        f"session 内存态 / 配额回报 / onChanged 真实写入 / 内部键隔离)"
        f" + SW({len(kernel_sw_facts())} 项：onStartup 每会话一次 / 注册失败有界自愈)"
        f" + 混淆契约 {keep_count} 条 + 无假保活"
        f" + 跨语言权限契约(C++ PermVerdict {perm_stats['enum_pairs']} ↔ Java PERM_* "
        f"{perm_stats['java_consts']} 数值 1:1；Java 自校验权限 {perm_stats['java_checked_perms']} "
        f"条逐条 verdict+capability；负向回归夹具已通过)"
        f" + 契约产物同步(v{contract_stats['schema_version']} digest={contract_stats['digest'][:16]} "
        f"epoch={contract_stats['epoch']}；C++/Java/JSON 与源码同源)"
        f" + 结构化自检(K-06：{len(selftest)} 项接线成立，JSON pass/fail/retryable/failures"
        f"+contractDigest，JNI↔Java 门面齐备)")
    return {"window_keys": declared_keys, "code_pairs": len(pairs),
            "entrypoints": len(facade["runtime_entrypoints"]), "keep_rules": keep_count}


def source_facts() -> dict:
    """从源码派生全部事实（不写文件）。"""
    routing = kernel_routing_facts()
    return {
        "routing": routing,
        "mv3_permissions": kernel_mv3_permissions(),
        "netdisk_schemas": kernel_netdisk_schema_facts(),
        "events": {
            "host_fireable": routing["host_fireable_events"],
            "dispatched_in_kernel": kernel_dispatched_events(),
        },
        "worker_window": kernel_worker_window_facts(),
        "wake_outbox": kernel_wake_facts(),
        "java_facade": kernel_java_facade_facts(),
        "dnr": kernel_dnr_facts(),
    }


def verified_facts() -> dict:
    facts = source_facts()
    verify_capability_claims(facts["routing"], facts["mv3_permissions"],
                             facts["netdisk_schemas"], facts["worker_window"],
                             facts["wake_outbox"], facts["java_facade"])
    return facts


# ── 清单 ────────────────────────────────────────────────────────────────────
def capability_manifest_data(ver: str, n: int, locked: dict, abi: dict) -> dict:
    """清单数据装配（出包与独立核查共用同一口径）——门禁在这里强制跑。"""
    routing = kernel_routing_facts()
    permissions = kernel_mv3_permissions()
    schemas = kernel_netdisk_schema_facts()
    window = kernel_worker_window_facts()
    wakes = kernel_wake_facts()
    facade = kernel_java_facade_facts()
    verify_capability_claims(routing, permissions, schemas, window, wakes, facade)
    sources = [p.as_posix() for p in (REGISTRY_JSON, REGISTRY_HEADER, ROUTING_HEADER,
                                      MV3_PERMISSION_MAP, MV3_PERMISSION_MAP_H,
                                      JAVA_MV3_GUARD, CONTRACT_JSON, CONTRACT_HEADER,
                                      CONTRACT_JAVA, LISTENER_REGISTRY,
                                      WORKER_WINDOW_HEADER, WAKE_OUTBOX_HEADER,
                                      WAKE_OUTBOX_SOURCE, SW_HOST_SOURCE,
                                      SELF_TEST_SOURCE, SELF_TEST_H, MV3_GUARD_JNI,
                                      JAVA_BRIDGE_FACADE,
                                      DNR_SCHEMA, DNR_RULES_HEADER, EXT_API_HOST,
                                      REINJECT_SOURCE, REINJECT_HEADER,
                                      LOADER_BRIDGE_SOURCE, PLUGIN_BUILD_GN,
                                      PLUGIN_STORAGE_H, PLUGIN_STORAGE_CC,
                                      STORAGE_HOST, STORAGE_MOJOM, STORAGE_IPC,
                                      ALARMS_SOURCE)]
    sources += [d.as_posix() for d in _netdisk_schema_paths(_read_registry())
                if (SRC / d).is_file()]
    return {
        "schema": "arupa.capability-manifest/1",
        "generated": time.strftime("%Y-%m-%d"),
        "platform": "android",
        "kernel_version": ver,
        "delivery_id": f"{ver}+{n}",
        "abi": abi,
        "locked_sha256": locked,
        # 下面各段都是从内核源码解析出的真实表项/常量（供宿主与验收核对接口面）。
        # 这里没有 capabilities/namespaces 那种"声明式布尔"：声明只能由本清单从源码
        # 派生出来，available 只能由运行期事实给出（见 runtime_facts）。
        "routing": routing,
        "mv3_permissions": permissions,
        "events": {
            "host_fireable": routing["host_fireable_events"],
            "dispatched_in_kernel": kernel_dispatched_events(),
            "netdisk_permission_gated": routing["netdisk"]["permission_gated_events"],
            "note": "host_fireable 是「能发」，dispatched_in_kernel 是「会分发且会被 "
                    "listExtListeners 判 honored」；两者对网盘事件必须一致。",
        },
        "aliases": {
            "namespaces": sorted(schemas),
            "permission": "nomadTransfer",
            "capability": "cap.nomad.invoke",
            "schemas": {ns: schemas[ns]["schema"] for ns in sorted(schemas)},
            "note": "nomad 与 nomadTransfer 是同一能力的两条命名：同一权限、同一校验、"
                    "同一错误码；声明任一条都只授予 nomadTransfer 能力，别名不扩权限面。",
        },
        "netdisk_schemas": schemas,
        "worker_window": window,
        "wake_outbox": wakes,
        # 净化(S-01/S-03/S-04)：DNR 面的可核对事实（方法三处同源、错误码、限额）。
        "declarative_net_request": kernel_dnr_facts(),
        # 阅读(R-03)：补注入面的可核对事实（启用路径/代次判活/机读回执/编译接线）。
        "content_script_reinject": kernel_reinject_facts(),
        # chrome.storage.local/session/onChanged 的可核对事实（区语义/配额回报/
        # onChanged 真实性/内部键隔离）。
        "chrome_storage": kernel_storage_facts(),
        # Service worker：onStartup 每会话一次 / 注册失败有界自愈。
        "service_worker": kernel_sw_facts(),
        "runtime_facts": {
            "note": "available/verified 只能由运行期事实给出（Java 门面 "
                    "reserveWorkerWindow() / nomadTransferCapabilities() / "
                    "pendingNomadTransferWakes() / extensionRuntimeSnapshot()），"
                    "并由设备证据验证；本静态清单只报源码里真实存在的路由、事件、"
                    "常量与门面入口，不把「有代码路径」写成「已通过」。",
            "entrypoints": facade["runtime_entrypoints"],
            "sources": sources,
        },
        "declaration_vs_implementation": {
            "gate": "scripts/kernel_capability.py::verify_capability_claims（出包前强制）",
            "checked": ["schema↔路由（含逐方法）", "schema↔事件白名单（双向）",
                        "事件↔内核分发表（双向）", "nomad/nomadTransfer 别名成对且同能力",
                        "窗口预算自洽与 v2.4 契约", "C++↔Java 结果码 1:1",
                        "K-02：C++ PermVerdict ↔ Java PERM_* 数值 1:1",
                        "K-02：C++ 权限表 ↔ Java expectPerm 逐条 verdict+capability（含负向回归夹具）",
                        "K-03：AAR 内嵌契约 JSON ↔ 源码派生契约逐字段核对（打包强制）",
                        "K-04：契约产物(C++ 头/Java 常量/JSON)与源码同步 + 启动 O(1) 身份比对",
                        "K-05：跨 ABI 内嵌契约 digest/schema 一致",
                        "运行期事实入口齐备", "清单窗口键名↔运行期 JSON 键名",
                        "DNR：schema↔支持表↔分发 三处同源（含逐方法分支）",
                        "DNR：E_DNR_* 错误码全部可达且错误回执结构化",
                        "DNR：schema properties↔内核常量数值一致",
                        "R-03：启用跳变才补注入（宿主重复调启用不双重注入）",
                        "R-03：停用/卸载作废代次，迟到回执按代次丢弃",
                        "R-03：逐帧判权 + 机读回执（verdict）+ 编译目标不成环",
                        "storage：session 区不落盘（重启即空）+ 分区配额符合 Chrome 口径",
                        "storage：写入结果经 mojom/IPC 一路回给扩展（不再静默超配额）",
                        "storage：onChanged 只对真实写入派发 + managed 只读 + 内核内部键不进"
                        "扩展可见区",
                        "SW：runtime.onStartup 每浏览器会话一次（不是每次 SW 启动都发）",
                        "SW：注册失败有界自愈（最多 2 次，成功后计数归零）",
                        "交付件 consumer keep 规则齐备（App 构建会逐条校验）",
                        "无假保活：SW 生命周期只由窗口在持引用驱动",
                        "K-06：结构化自检结果（JSON pass/fail/retryable/failures + "
                        "contractDigest，JNI↔Java 门面齐备）"],
            "passed": True,
            "note": "只证明静态声明与内核实现同源（回应 N-C08/K-A09）；设备上是否真可用"
                    "由 runtime_facts 的入口与设备用例给出。",
        },
    }


def write_capability_manifest(stage: Path, ver: str, n: int,
                              abi_major: int = 1, abi_minor: int = 24) -> Path:
    """给交付目录写 capability-manifest.json（必须在算 SHA256SUMS 之前调用）。"""
    stage = Path(stage).resolve()
    if not (stage / "kernel").is_dir():
        err(f"{stage} 下没有 kernel/ —— 不是交付目录，拒绝写清单")
    locked = {p.relative_to(stage / "kernel").as_posix(): _sha256_file(p)
              for p in sorted((stage / "kernel").rglob("*")) if p.is_file()}
    data = capability_manifest_data(ver, n, locked, {"major": abi_major, "minor": abi_minor})
    out = stage / "capability-manifest.json"
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"  生成: capability-manifest.json（锁定 {len(locked)} 件；"
        f"路由前缀 {len(data['routing']['host_owned_prefixes'])} 条，"
        f"权限 {len(data['mv3_permissions'])} 条，"
        f"事件 {len(data['events']['host_fireable'])} 条（其中网盘 "
        f"{len(data['events']['netdisk_permission_gated'])} 条），"
        f"窗口租约 {data['worker_window']['max_lease_ms']}ms）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="内核能力清单与声明↔实现同源门禁")
    ap.add_argument("cmd", nargs="?",
                    choices=("facts", "gate", "manifest", "contract", "verify-aar"),
                    default="facts")
    ap.add_argument("--stage", help="交付目录（manifest 用；其下应有 kernel/）")
    ap.add_argument("--ver", help="内核版本（manifest 用）")
    ap.add_argument("--num", type=int, help="交付编号（manifest 用）")
    ap.add_argument("--abi-major", type=int, default=1)
    ap.add_argument("--abi-minor", type=int, default=24)
    ap.add_argument("--write", action="store_true",
                    help="contract：把契约产物写回源码树（不写则只打印 digest）")
    ap.add_argument("--out-dir", help="contract：输出目录（默认写回源码树）")
    ap.add_argument("--aar", action="append", default=[],
                    help="verify-aar：AAR 路径（可重复；≥2 时额外做跨 ABI 契约比对）")
    args = ap.parse_args()

    if args.cmd == "manifest":
        if not args.stage or not args.ver or args.num is None:
            err("manifest 需要 --stage <交付目录> --ver <版本> --num <编号>")
        log(f"生成能力清单: stage={args.stage} ver={args.ver} num={args.num}")
        out = write_capability_manifest(args.stage, args.ver, args.num,
                                        args.abi_major, args.abi_minor)
        print(str(out))
        return 0
    if args.cmd == "gate":
        log("内核声明 ↔ 实现同源门禁（只读源码，不需要构建产物）")
        verified_facts()
        return 0
    if args.cmd == "contract":
        doc = build_contract()
        if args.write or args.out_dir:
            root = Path(args.out_dir).resolve() if args.out_dir else None
            if root is not None:
                # 写到指定目录（GN/打包在构建目录里取用），仍按 ROOT 相对路径落盘。
                for rel, text in ((CONTRACT_JSON, _render_contract_json(doc)),
                                  (CONTRACT_HEADER, _render_contract_header(doc)),
                                  (CONTRACT_JAVA, _render_contract_java(doc))):
                    p = root / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text, encoding="utf-8")
                log(f"  契约产物已写出到 {root}: digest={doc['contractDigest'][:16]}")
            else:
                write_contract(WORKSPACE_ROOT / "src")
        print(json.dumps({"schemaVersion": doc["schemaVersion"],
                          "contractEpoch": doc["contractEpoch"],
                          "contractDigest": doc["contractDigest"]},
                         ensure_ascii=False))
        return 0
    if args.cmd == "verify-aar":
        if not args.aar:
            err("verify-aar 需要 --aar <路径>（可重复）")
        expected = build_contract()
        results = [verify_aar_contract(Path(p), expected) for p in args.aar]
        if len(args.aar) >= 2:
            cross = verify_cross_abi_contract([Path(p) for p in args.aar])
            log(f"  跨 ABI 契约一致: {cross['count']} 份 · digest={cross['digest'][:16]}")
        log(f"  AAR 内嵌契约与源码同源: {len(results)} 份（digest={expected['contractDigest'][:16]}）")
        return 0
    # 说明走 stderr，stdout 只留 JSON（便于 `| python3 -c 'json.load(...)'` 直接用）。
    warn("内核源码事实（只读源码，不需要构建产物）；"
         "用法：python3 scripts/kernel_capability.py [facts|gate|manifest|contract|verify-aar]")
    print(json.dumps(verified_facts(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
