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
AUDIT_EVENT_PREFIXES = ("nomad.sitePermissionAudit.", "sitePermissionAudit.")
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
EXT_API_MOJOM = ROOT / "plugin/mojom/arupa_ext_api.mojom"
EXT_API_IPC = ROOT / "plugin/arupa_ipc_message_sender.cc"
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
MESSAGE_CHANNEL_SOURCE = ROOT / "plugin/arupa_message_channel.cc"
LOCAL_FRAME_HOST_SOURCE = ROOT / "plugin/arupa_local_frame_host.cc"
PLUGIN_JNI_SOURCE = ROOT / "plugin/plugin_jni.cc"
WEB_REQUEST_SOURCE = ROOT / "plugin/arupa_web_request_events.cc"
INTERCEPT_LOADER_SOURCE = ROOT / "aar/native/arupa_intercept_loader.cc"
CONTENT_BROWSER_CLIENT_SOURCE = ROOT / "aar/native/arupa_content_browser_client.cc"
# chrome.storage.local / session / onChanged 的落点：后端 + API 分发 + 宿主 + mojom 面。
PLUGIN_STORAGE_H = ROOT / "mv3/plugin_storage.h"
PLUGIN_STORAGE_CC = ROOT / "mv3/plugin_storage.cc"
STORAGE_HOST = ROOT / "plugin/arupa_plugin_storage_host.cc"
STORAGE_MOJOM = ROOT / "plugin/mojom/arupa_plugin_storage.mojom"
STORAGE_IPC = ROOT / "plugin/arupa_ipc_message_sender.cc"
ALARMS_SOURCE = ROOT / "plugin/arupa_alarms.cc"
PRODUCT_CAPABILITY_CANDIDATES = tuple(
    WORKSPACE_ROOT / "nomad_android/app/src/main/java/"
    "com/nomadbrowser/app/pluginruntime" / name
    for name in (
        # +64 current branch: the declaration sent to the kernel at runtime.
        "ArupaHostCapabilities.kt",
        # Legacy/full-capability product branches.
        "ArupaAndroidExtensionCapabilities.kt",
    )
)

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
    """去掉 C/C++ 注释，但保留字符串中的 ``//``、``/*`` URL pattern。

    API schema/host 校验代码包含 ``https://`` 和 ``*://*/*``。正则删注释会从
    字符串里的 ``/*`` 一直吞到源码中下一个 ``*/``，令门禁误判后半个文件根本
    不存在。这里使用一个小型词法扫描器；它不解释 C++，只区分字符串、字符、
    行注释和块注释，足够满足事实提取且保持换行位置稳定。
    """
    out: list[str] = []
    i = 0
    state = "code"
    quote = ""
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if ch in {'"', "'"}:
                state = "quoted"
                quote = ch
                out.append(ch)
                i += 1
            elif ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                i += 2
            else:
                out.append(ch)
                i += 1
        elif state == "quoted":
            out.append(ch)
            i += 1
            if ch == "\\" and i < len(text):
                out.append(text[i])
                i += 1
            elif ch == quote:
                state = "code"
        elif state == "line_comment":
            if ch == "\n":
                out.append(ch)
                state = "code"
            i += 1
        else:  # block_comment
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
            else:
                if ch == "\n":
                    out.append(ch)
                i += 1
    return "".join(out)


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
    dependencies = reg["routing"]["renderer_host_capability_dependencies"]
    dependency_strings = [item for host, members in dependencies.items()
                          for member in members for item in (host, member)]
    derived_methods = [member for members in dependencies.values()
                       for member in members]
    text = _strip_comments(_read_source(REGISTRY_HEADER))
    pairs = [
        ("kHostOwnedPrefixes", reg["routing"]["host_owned_prefixes"]),
        ("kHostOwnedMethods", reg["routing"]["host_owned_methods"]),
        ("kKernelHostBridgeMethods",
         reg["routing"]["kernel_host_bridge_methods"]),
        ("kRendererHostCapabilityMethods",
         reg["routing"]["renderer_host_capability_methods"]),
        ("kRendererHostCapabilityEvents",
         reg["routing"]["renderer_host_capability_events"]),
        ("kRendererHostCapabilityDependencies", dependency_strings),
        ("kRendererDerivedHostCapabilityMethods", derived_methods),
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
    host_methods = routing["host_owned_methods"]
    kernel_host_methods = routing["kernel_host_bridge_methods"]
    renderer_host_methods = routing["renderer_host_capability_methods"]
    renderer_host_events = routing["renderer_host_capability_events"]
    renderer_dependencies = routing["renderer_host_capability_dependencies"]
    kernel = routing["kernel_owned_bridge_prefixes"]
    events = routing["host_fireable_events"]
    _check_table("host_owned_prefixes", host, suffix=".")
    _check_table("host_owned_methods", host_methods)
    _check_table("kernel_host_bridge_methods", kernel_host_methods)
    _check_table("renderer_host_capability_methods", renderer_host_methods)
    _check_table("renderer_host_capability_events", renderer_host_events)
    _check_table("kernel_owned_bridge_prefixes", kernel)
    _check_table("host_fireable_events", events)
    for table_name, entries in (("host_owned_methods", host_methods),
                                ("kernel_host_bridge_methods", kernel_host_methods),
                                ("renderer_host_capability_methods", renderer_host_methods),
                                ("renderer_host_capability_events", renderer_host_events),
                                ("host_fireable_events", events)):
        if len(entries) != len(set(entries)):
            err(f"{table_name} 含重复成员，精确合同不允许歧义。")
    exact_methods = set(host_methods) | set(kernel_host_methods)
    unknown_renderer_methods = sorted(set(renderer_host_methods) - exact_methods)
    unknown_renderer_events = sorted(set(renderer_host_events) - set(events))
    dependency_hosts = set(renderer_dependencies)
    derived_renderer_methods = [member
                                for members in renderer_dependencies.values()
                                for member in members]
    unknown_dependency_hosts = sorted(dependency_hosts - exact_methods)
    overlapping_renderer_methods = sorted(
        set(derived_renderer_methods) & set(renderer_host_methods))
    if (len(derived_renderer_methods) != len(set(derived_renderer_methods)) or
            unknown_dependency_hosts or overlapping_renderer_methods):
        err("renderer 派生 capability 依赖含重复成员或未知宿主方法:\n  " +
            "\n  ".join(unknown_dependency_hosts +
                        overlapping_renderer_methods +
                        derived_renderer_methods))
    if unknown_renderer_methods or unknown_renderer_events:
        err("renderer 动态可见性表不是精确宿主合同的子集:\n  " +
            "\n  ".join(unknown_renderer_methods + unknown_renderer_events))
    bad_host_methods = [m for m in host_methods if not _has_prefix(host, m)]
    bad_kernel_host_methods = [m for m in kernel_host_methods
                               if not _has_prefix(kernel, m)
                               and not _has_prefix(host, m)]
    if bad_host_methods or bad_kernel_host_methods:
        err("精确宿主成员没有对应路由分类:\n  " +
            "\n  ".join(bad_host_methods + bad_kernel_host_methods))
    if not host or not events:
        err("登记表路由解析结果为空 —— 登记表与解析器已漂移，先修登记表再出包。")
    protos = _registry_protocol_namespaces(reg)
    proto_suffixes = {ns + "." for ns in protos}
    netdisk_prefixes = [p for p in host if p in proto_suffixes]
    # 审计通知由内核内部派发，不能出现在宿主主动投递白名单里；尤其
    # nomad.sitePermissionAudit.* 虽以 nomad. 开头，也不属于网盘权限面。
    audit_host_events = [e for e in events if e.startswith(AUDIT_EVENT_PREFIXES)]
    if audit_host_events:
        err("独立审计事件不得由宿主主动投递: " + ", ".join(audit_host_events))
    netdisk_events = [e for e in events
                      if any(e.startswith(s) for s in proto_suffixes)
                      and not e.startswith(AUDIT_EVENT_PREFIXES)]
    facts = {
        "host_owned_prefixes": host,
        "host_owned_methods": host_methods,
        "kernel_host_bridge_methods": kernel_host_methods,
        "renderer_host_capability_methods": renderer_host_methods,
        "renderer_host_capability_events": renderer_host_events,
        "renderer_host_capability_dependencies": renderer_dependencies,
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


def kernel_edge_android_api_facts() -> dict:
    """Collect browser-side members for the Edge Android MV3 target surface.

    Local JSON schemas are the actual Arupa binding surface. Three upstream
    JSON schemas are also bundled/used directly; system.cpu/memory are WebIDL
    generated by //extensions/common and therefore listed explicitly here.
    Renderer-only/nocompile functions (for example i18n.getMessage) are not
    browser routes and are intentionally excluded.
    """
    schema_paths = sorted((SRC / ROOT / "plugin/api").glob("*.json"))
    schema_paths += [
        WORKSPACE_ROOT / "src/extensions/common/api/i18n.json",
        WORKSPACE_ROOT / "src/chrome/common/extensions/api/sessions.json",
        WORKSPACE_ROOT / "src/chrome/common/extensions/api/declarative_content.json",
        WORKSPACE_ROOT / "src/chrome/common/extensions/api/permissions.json",
    ]
    methods = set()
    namespaces = set()
    for path in schema_paths:
        if not path.is_file():
            err(f"Edge Android API schema missing: {path}")
        schema_text = re.sub(r"(?m)^\s*//.*$", "",
                             path.read_text(encoding="utf-8"))
        for namespace in json.loads(schema_text):
            ns = namespace.get("namespace")
            if not ns:
                continue
            namespaces.add(ns)
            for function in namespace.get("functions", []):
                if function.get("nocompile"):
                    continue
                methods.add(f"{ns}.{function['name']}")
    # Chromium M154 把下列 API 放在 IDL/WebIDL；只扫 JSON 会把它们
    # 整个跳过，门禁就无法证明真实 binding 面。这里只抽取 API
    # interface/namespace 中的 static 方法，不把 event listener 的
    # addListener/removeListener 接口当成 browser method。
    webidl_sources = {
        "alarms": WORKSPACE_ROOT / "src/extensions/common/api/alarms.webidl",
        "scripting": WORKSPACE_ROOT / "src/extensions/common/api/scripting.idl",
        "userScripts": WORKSPACE_ROOT / "src/extensions/common/api/user_scripts.webidl",
        "system.cpu": WORKSPACE_ROOT / "src/extensions/common/api/system_cpu.webidl",
        "system.memory": WORKSPACE_ROOT / "src/extensions/common/api/system_memory.webidl",
    }
    for namespace, path in webidl_sources.items():
        if not path.is_file():
            err(f"Edge Android WebIDL source missing: {path}")
        text = _strip_comments(path.read_text(encoding="utf-8"))
        declared = set(re.findall(
            r"\bstatic\s+(?!attribute\b)[^;{}]*?\b([A-Za-z_]\w*)\s*\(",
            text, flags=re.S))
        # ExtensionEvent interface 也有 static add/remove/hasListener，它们不是
        # namespace browser function。
        declared -= {"addListener", "removeListener", "hasListener"}
        if not declared:
            err(f"Edge Android WebIDL parser found no methods in {path}")
        namespaces.add(namespace)
        methods.update(f"{namespace}.{method}" for method in declared)

    # alarms/scripting/userScripts are WebIDL sources and dom is renderer-only;
    # none adds a JSON browser route here, but each must still be accounted for
    # in the exact Edge Android MV3 namespace baseline.
    exact_edge_mv3_namespaces = {
        "action", "alarms", "browsingData", "contentSettings", "cookies",
        "declarativeContent", "declarativeNetRequest", "dom", "downloads",
        "extension", "history", "i18n", "identity", "idle", "management",
        "notifications", "offscreen", "permissions", "runtime", "scripting",
        "search", "sessions", "storage", "system.cpu", "system.memory", "tabs",
        "userScripts", "webNavigation", "webRequest",
    }
    declared_edge_namespaces = namespaces | {
        "alarms", "dom", "idle", "management", "runtime", "scripting",
        "storage", "userScripts", "webRequest",
    }
    missing_edge_namespaces = exact_edge_mv3_namespaces - declared_edge_namespaces
    if missing_edge_namespaces:
        err("Edge Android MV3 namespaces missing from the Arupa declaration surface:\n  "
            + "\n  ".join(sorted(missing_edge_namespaces)))
    return {"namespaces": sorted(namespaces), "methods": sorted(methods),
            "edge_namespaces": sorted(exact_edge_mv3_namespaces)}


def verify_edge_android_api_routes(routing: dict) -> dict:
    """Every browser-side member in the Android binding surface must route.

    This is intentionally independent of the generic host being installed:
    build-time declarations must have either a kernel route or a host route;
    runtime member availability is then intersected with the versioned host
    capability contract.
    """
    facts = kernel_edge_android_api_facts()
    exact_host_methods = set(routing["host_owned_methods"])
    derived_renderer_methods = {
        method
        for methods in routing["renderer_host_capability_dependencies"].values()
        for method in methods
    }
    # storage.sync / storage.managed 是 storage schema 中的动态子对象，
    # 不是独立 browser method。它们仍必须作为中间 Feature 登记，
    # 否则 storage.managed.get 会被 native bindings 误当成独立 API，
    # Renderer 在 GetAPISchema("storage.managed.get") 处直接 FATAL。
    storage_schema = _read_source(
        WORKSPACE_ROOT / "src/extensions/common/api/storage.json")
    derived_api_objects = {
        f"storage.{area}" for area in ("sync", "managed")
        if re.search(rf'"{area}"\s*:\s*\{{\s*"\$ref"\s*:\s*"StorageArea"',
                     storage_schema)
    }
    unknown_derived_methods = sorted(
        derived_renderer_methods - set(facts["methods"]) - derived_api_objects)
    if unknown_derived_methods:
        err("renderer 派生 capability 指向未声明的 browser method:\n  " +
            "\n  ".join(unknown_derived_methods))
    provider = _read_source(
        SRC / ROOT / "plugin/arupa_extensions_api_provider.cc")
    unavailable_members = set(re.findall(
        r'ARUPA_API_UNAVAILABLE\(\s*"([^"]+)"\s*\)', provider))
    missing = [
        method for method in facts["methods"]
        if method not in unavailable_members
        if method not in exact_host_methods
        and not _has_prefix(routing["kernel_owned_bridge_prefixes"], method)
    ]
    if missing:
        err("Edge Android schema declares browser-side members with no kernel/host route:\n  "
            + "\n  ".join(missing))
    dispatched = kernel_dispatched_events()
    missing_events = [event for event in routing["host_fireable_events"]
                      if event not in dispatched]
    if missing_events:
        err("Host-fireable events are not honored by the listener registry:\n  "
            + "\n  ".join(missing_events))
    # These namespaces come from Chrome-layer feature/permission tables that
    # the compact Android client does not install.  A schema and route alone
    # are insufficient: without both a permission feature and an API
    # dependency the namespace is either unreachable or exposed to an
    # extension that did not request it.
    permission_dependencies = {
        "search": "kSearchPermissionDep",
        "sessions": "kSessionsPermissionDep",
        "declarativeContent": "kDeclarativeContentPermissionDep",
        "system.cpu": "kSystemCpuPermissionDep",
        "system.memory": "kSystemMemoryPermissionDep",
    }
    for namespace, dependency in permission_dependencies.items():
        api_pattern = (rf'ARUPA_API_DEP\(\s*"{re.escape(namespace)}"\s*,\s*'
                       rf'{dependency}\s*\)')
        perm_pattern = rf'ARUPA_PERM\(\s*"{re.escape(namespace)}"\s*,'
        if not re.search(api_pattern, provider):
            err(f"Edge Android API {namespace} is not bound to {dependency}.")
        if not re.search(perm_pattern, provider):
            err(f"Edge Android permission feature {namespace} is not registered.")
    for member in (
            "browserAction", "pageAction", "webNavigation.onTabReplaced",
            "permissions.addHostAccessRequest",
            "permissions.removeHostAccessRequest"):
        if not re.search(rf'ARUPA_API_UNAVAILABLE\(\s*"{re.escape(member)}"\s*\)',
                         provider):
            err(f"Unsupported Android/MV3 member must stay unavailable: {member}")
    return facts


def verify_round2_host_contract(routing: dict) -> dict:
    """R2-01..14 的可机读接线门禁。

    这不企图用文本扫描代替 C++/Android 测试；它的作用是防止修复后
    某一层（精确登记表、renderer 快照、validator、取消 ACK、journal）
    在后续重构中单边丢失。
    """
    host = _read_source(EXT_API_HOST)
    host_h = _read_source(ROOT / "plugin/arupa_ext_api_host.h")
    prefixes = _read_source(ROUTING_HEADER)
    loader = _read_source(LOADER_BRIDGE_SOURCE)
    provider = _read_source(ROOT / "plugin/arupa_extensions_api_provider.cc")
    client = _read_source(ROOT / "plugin/arupa_extensions_client.cc")
    jni = _read_source(ROOT / "plugin/plugin_jni.cc")
    java = _read_source(JAVA_BRIDGE_FACADE)
    tabs = _read_source(ROOT / "plugin/arupa_ext_api_tabs.cc")
    web_navigation = _read_source(
        ROOT / "plugin/arupa_web_navigation_events.cc")
    webview_delegate = _read_source(
        ROOT / "aar/native/arupa_webview_delegate.cc")
    browsing = _read_source(ROOT / "plugin/arupa_browsing_data.cc")
    cookies = _read_source(ROOT / "plugin/arupa_ext_api_cookies.cc")
    tests = _read_source(ROOT / "tests/arupa_edge_api_contract_unittest.cc")
    runtime_hooks_path = (WORKSPACE_ROOT /
                          "src/extensions/renderer/api/runtime_hooks_delegate.cc")
    if not runtime_hooks_path.is_file():
        err(f"renderer runtime hook missing: {runtime_hooks_path}")
    runtime_hooks = runtime_hooks_path.read_text(encoding="utf-8")

    exact_methods = (routing["host_owned_methods"] +
                     routing["kernel_host_bridge_methods"])
    if len(exact_methods) != len(set(exact_methods)):
        err("公开/内部宿主精确成员表之间有重复。")

    # 每个精确成员必须落在 ValidateHostApiResult 的一个明确模型
    # family 中；新增成员未分类时直接拒绝出包。
    result_families = (
        "tabs.", "action.", "contextMenus.", "downloads.", "bookmarks.",
        "history.", "notifications.", "identity.", "i18n.", "search.",
        "sessions.", "declarativeContent.", "system.cpu.",
        "system.memory.", "fontSettings.", "power.", "sidePanel.",
        "cookies.", "storage.sync.", "storage.managed.",
        "types.ChromeSetting.",
        "nomadTransfer.", "nomad.", "extension.", "windows.",
        "browsingData.", "management.", "declarativeNetRequest.",
        "contentSettings.", "permissions.", "runtime.")
    unclassified = [m for m in exact_methods
                    if not any(m.startswith(p) for p in result_families)]
    if unclassified:
        err("精确宿主成员缺结果 validator family:\n  " +
            "\n  ".join(unclassified))

    event_families = ("tabs.", "downloads.", "bookmarks.",
                      "notifications.", "history.", "identity.", "action.",
                      "commands.",
                      "sidePanel.", "windows.",
                      "management.", "sessions.", "storage.",
                      "contentSettings.", "declarativeNetRequest.",
                      "nomad.", "nomadTransfer.")
    bad_events = [e for e in routing["host_fireable_events"]
                  if not any(e.startswith(p) for p in event_families)]
    if bad_events:
        err("宿主事件缺参数 validator family:\n  " +
            "\n  ".join(bad_events))
    if ("storage.sync.onChanged" in routing["host_fireable_events"] or
            "storage.managed.onChanged" in routing["host_fireable_events"]):
        err("storage area 事件不得作为第二条宿主入口。")

    required_markers = {
        "exact capability method registry":
            (prefixes, "return IsHostOwnedApi(name) || IsKernelHostBridgeApi(name)"),
        "capability v2": (host, "*version != 2"),
        "capability canonical digest": (host, "SHA256HashString(canonical_json)"),
        "capability kernel delivery digest":
            (host, "mv3::contract::kDigest"),
        "capability scope": (host, "capability scope does not match"),
        "renderer capability manifest":
            (host, "RendererHostCapabilityManifestData"),
        "renderer delegated availability":
            (provider, "requires_delegated_availability_check = true"),
        "renderer bridge capability checks":
            (client, "kRendererHostCapabilityMethods"),
        "renderer mixed member preservation":
            (tests, "RendererCapabilityOnlyGatesHostOnlyMembers"),
        "renderer derived host dependencies":
            (host, "kRendererHostCapabilityDependencies"),
        "renderer context refresh":
            (loader, "RefreshExtensionBindingsForHostCapabilities"),
        "member capability report":
            (host, "ExtensionApiCapabilityReportJson"),
        "JNI member capability report":
            (jni, "GetExtensionApiCapabilityReport"),
        "Java member capability report":
            (java, "getExtensionApiCapabilityReport"),
        "private manifest redaction":
            (runtime_hooks, "__arupa_host_capability_members"),
        "result fail closed":
            (host, "no registered host result validator"),
        "event fail closed":
            (host, "no registered host event validator"),
        "event v2 context": (host, "event context v2 is required"),
        "event generation replay guard":
            (host, "event source generation or sequence is stale"),
        "positive install generation":
            (host, "HOST_INSTALL_GENERATION_REQUIRED"),
        "legacy event API rejected":
            (jni, "legacy fireExtEventWithWake rejected"),
        "Java event v2 facade": (java, "fireExtEventWithWakeV2"),
        "tabs capture permission gate":
            (tabs, "CanCaptureVisiblePage"),
        "tabs retired book check": (tabs, "BookForRetiredTabId"),
        "web navigation created target stable ids":
            (tests, "WebNavigationCreatedTargetHasSchemaExactStableTabIdentity"),
        "web navigation deferred target dispatch":
            (web_navigation, "OnTabRegisteredForWebNavigation"),
        "auth challenge install generation":
            (webview_delegate, "install_generation"),
        "auth challenge runtime identity":
            (webview_delegate, "PendingWebRequestAuthIsCurrent"),
        "auth challenge exact book dispatch":
            (webview_delegate,
             "*worker_context, listener->dispatch_event_name"),
        "cookies CHIPS serialization":
            (cookies, 'd.Set("partitionKey", std::move(key))'),
        "cookies stable regular and incognito stores":
            (cookies, "StoreIdForBook"),
        "cookies partition-key frame identity":
            (cookies, 'op == "getPartitionKey"'),
        "browsingData unsupported preflight":
            (browsing, "serverBoundCertificates"),
        "cancellation registration contract":
            (host, "cancel listener"),
        "cancellation ACK":
            (host_h, "AcknowledgeExtensionApiCancellation"),
        "purge journal": (loader, "__arupa_extension_purge_journal_v1"),
        "uninstall ACK": (loader, "AckUninstallUrlDelivery"),
        "content setting ownership ledger":
            (host, "content_settings_ownership_v2"),
        "content setting compensation":
            (host, "FailContentSettingSetWithCompensation"),
        "content setting external event source":
            (host, "NotifyExternalContentSettingChanged"),
        "storage sync commit v2": (host, "StorageSync commit v2"),
        "storage revision replay guard":
            (host, "duplicate or stale storage revision"),
        "hardware independent consent":
            (host, "independent high-risk hardware-info consent required"),
        "exact registry regression tests":
            (tests, "HostCapabilityAcceptsOnlyGeneratedExactMembers"),
    }
    missing = [name for name, (text, marker) in required_markers.items()
               if marker not in text]
    if missing:
        err("二轮宿主合同接线缺失:\n  " + "\n  ".join(missing))
    return {"methods": len(exact_methods),
            "events": len(routing["host_fireable_events"]),
            "checks": len(required_markers)}


def verify_current_android_product_contract() -> dict:
    """CAP-02：核对当前逐风运行时真正发送给内核的 host API 合同。"""
    product_capabilities = next(
        (path for path in PRODUCT_CAPABILITY_CANDIDATES if path.is_file()), None)
    if product_capabilities is None:
        err("当前 Android 产品合同不存在，已检查:\n  " +
            "\n  ".join(str(path) for path in PRODUCT_CAPABILITY_CANDIDATES))
    text = product_capabilities.read_text(encoding="utf-8")

    def strings_in(name: str) -> set[str]:
        # Legacy/full contract: `internal val methods = listOf(...)`.
        match = re.search(rf"\b{name}\s*=\s*listOf\((.*?)\n\s*\)", text, re.S)
        if match:
            return set(re.findall(r'"([^"]+)"', match.group(1)))
        # +64 current contract: `.put("methods", JSONArray(listOf(...)))`.
        match = re.search(
            rf'\.put\("{re.escape(name)}"\s*,\s*JSONArray\(listOf\((.*?)\)\)\s*\)',
            text, re.S)
        if match:
            return set(re.findall(r'"([^"]+)"', match.group(1)))
        # An explicit empty JSONObject is the current representation for a
        # contract with no host-fireable events.
        if name == "events" and re.search(
                r'\.put\("events"\s*,\s*JSONObject\(\)\s*\)', text):
            return set()
        err(f"Android 产品合同无法解析 {name} 列表: {product_capabilities}")

    methods = strings_in("methods")
    events = strings_in("events")
    models_match = re.search(r"\bdataModels\s*=\s*linkedMapOf\((.*?)\n\s*\)", text, re.S)
    if models_match:
        models = {name: int(version) for name, version in re.findall(
            r'"([^"]+)"\s+to\s+(\d+)', models_match.group(1))}
    else:
        # +64 current contract builds the model object inline:
        # `.put("dataModels", JSONObject().put("Tab", 2)...)`.
        models_match = re.search(
            r'\.put\("dataModels"\s*,\s*JSONObject\(\)(.*?)\)\s*\n\s*\.put\(',
            text, re.S)
        if not models_match:
            err(f"Android 产品合同无法解析 dataModels: {product_capabilities}")
        models = {name: int(version) for name, version in re.findall(
            r'\.put\("([^"]+)"\s*,\s*(\d+)\)', models_match.group(1))}

    # 产品声明会在运行时被 SetExtApiCapabilitiesJson 按同一两张
    # 精确表验证。这里必须先做完全相同的子集校验，不能只验“最小集合”：
    # 否则内核自有成员被误报为 Android host 能力时，静态出包通过却会
    # 在设备启动阶段被原生合同拒绝。
    routing = kernel_routing_facts()
    exact_host_methods = (set(routing["host_owned_methods"]) |
                          set(routing["kernel_host_bridge_methods"]))
    unknown_methods = sorted(methods - exact_host_methods)
    unknown_events = sorted(events - set(routing["host_fireable_events"]))
    if unknown_methods or unknown_events:
        err("Android 产品合同声明了非精确宿主成员（CAP-02）:\n  " +
            "\n  ".join(unknown_methods + unknown_events))

    formal = {
        "tabs": ({"tabs.query"}, set(), {"Tab": 2}),
        "bookmarks": ({
            "bookmarks.create", "bookmarks.get", "bookmarks.getChildren",
            "bookmarks.getRecent", "bookmarks.getSubTree", "bookmarks.getTree",
            "bookmarks.move", "bookmarks.remove", "bookmarks.removeTree",
            "bookmarks.search", "bookmarks.update",
        }, {
            "bookmarks.onChanged", "bookmarks.onChildrenReordered",
            "bookmarks.onCreated", "bookmarks.onMoved", "bookmarks.onRemoved",
        }, {"BookmarkTreeNode": 1}),
        "notifications": ({
            "notifications.clear", "notifications.create", "notifications.getAll",
            "notifications.getPermissionLevel", "notifications.update",
        }, {
            "notifications.onClicked", "notifications.onClosed",
            "notifications.onButtonClicked",
            "notifications.onPermissionLevelChanged", "notifications.onShowSettings",
        }, {"Notification": 1}),
        "downloads": ({
            "downloads.cancel", "downloads.download", "downloads.erase",
            "downloads.getFileIcon", "downloads.open", "downloads.pause",
            "downloads.removeFile", "downloads.resume", "downloads.search",
            "downloads.show", "downloads.showDefaultFolder",
        }, {"downloads.onChanged", "downloads.onCreated", "downloads.onErased"},
            {"DownloadItem": 1}),
        "contentSettings": ({"contentSettings.get", "contentSettings.set"},
            {"contentSettings.onChanged"}, {"ContentSetting": 1}),
        "browsingData": ({
            "browsingData.remove", "browsingData.removeDownloads",
            "browsingData.removeFormData", "browsingData.removeHistory",
        }, set(), {}),
        "history": ({
            "history.search", "history.getVisits", "history.addUrl",
            "history.deleteUrl", "history.deleteRange", "history.deleteAll",
        }, {"history.onVisited", "history.onVisitRemoved"}, {"HistoryItem": 1}),
        "identity.email": ({"identity.getProfileUserInfo"},
            {"identity.onSignInChanged"}, {"IdentityProfile": 1}),
        "identity.webAuth": ({"identity.launchWebAuthFlow"}, set(), {}),
        "i18n": ({"i18n.getAcceptLanguages"}, set(), {"LocaleList": 1}),
        "windows": ({
            "windows.get", "windows.getCurrent", "windows.getLastFocused",
            "windows.getAll", "windows.create", "windows.update",
            "windows.remove",
        }, {
            "windows.onCreated", "windows.onRemoved",
            "windows.onFocusChanged", "windows.onBoundsChanged",
        }, {"Window": 2}),
        "search": ({"search.query"}, set(), {"SearchProvider": 1}),
        "sessions": ({"sessions.getRecentlyClosed", "sessions.restore"},
            {"sessions.onChanged"}, {"Session": 1}),
        "storage.managed": ({
            "storage.managed.get", "storage.managed.getKeys",
            "storage.managed.getBytesInUse",
        }, {"storage.onChanged"}, {"StorageManaged": 1, "StorageChange": 2}),
        "system.cpu": ({"system.cpu.getInfo"}, set(), {"CpuInfo": 1}),
        "system.memory": ({"system.memory.getInfo"}, set(), {"MemoryInfo": 1}),
        "declarativeContent": ({
            "declarativeContent.onPageChanged.addRules",
            "declarativeContent.onPageChanged.getRules",
            "declarativeContent.onPageChanged.removeRules",
        }, set(), {"DeclarativeRule": 1}),
        "management": ({
            "management.setEnabled",
            "management.uninstall", "management.uninstallSelf",
        }, {
            "management.onInstalled", "management.onUninstalled",
            "management.onEnabled", "management.onDisabled",
        }, {"ExtensionInfo": 1}),
    }
    incomplete_families = []
    for family, (need_methods, need_events, need_models) in formal.items():
        missing_methods = sorted(need_methods - methods)
        missing_events = sorted(need_events - events)
        missing_models = sorted(
            f"{name}@{version}" for name, version in need_models.items()
            if models.get(name, 0) < version)
        if missing_methods or missing_events or missing_models:
            incomplete_families.append(family)
    complete_families = len(formal) - len(incomplete_families)
    if incomplete_families:
        # A reduced product contract is legitimate. Keep reporting it as
        # reduced instead of inventing members that have no Android host.
        warn("当前 Android 产品合同仅覆盖部分正式族（CAP-02）: " +
             f"{complete_families}/{len(formal)} complete; incomplete=" +
             ",".join(incomplete_families))

    # management 的查询/警告成员由内核直接实现，不属于 Android
    # host 合同；仍要在这里固定它们的内核路由，避免“从 host 声明删掉”
    # 被误解成“产品能力删掉”。
    kernel_management = {
        "management.get", "management.getAll", "management.getSelf",
        "management.getPermissionWarningsById",
        "management.getPermissionWarningsByManifest",
    }
    missing_kernel_management = sorted(
        member for member in kernel_management
        if not _has_prefix(routing["kernel_owned_bridge_prefixes"], member))
    if missing_kernel_management:
        err("management 正式内核成员缺路由（CAP-02）:\n  " +
            "\n  ".join(missing_kernel_management))
    return {"source": product_capabilities.name,
            "methods": len(methods), "events": len(events), "models": len(models),
            "formal_families_complete": complete_families,
            "formal_families_total": len(formal),
            "incomplete_families": incomplete_families}


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
    substituted = sorted(
        permission for permission, row in cpp_perms.items()
        if row["verdict"] == "kSubstituted"
    )
    if substituted:
        fails.append(
            "受限替代权限必须先补齐正式闭环或明确降级，当前仍有: "
            + ", ".join(substituted)
        )
    if fails:
        err("跨语言权限契约不一致（K-02；正是 K-01 的量产通道）：\n  · "
            + "\n  · ".join(fails))
    return {"enum_pairs": len(cpp_enum),
            "java_consts": len(java_consts),
            "java_checked_perms": len(java_expects),
            "substituted_permissions": 0}


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
            "hostOwnedMethods": sorted(routing["host_owned_methods"]),
            "kernelHostBridgeMethods": sorted(
                routing["kernel_host_bridge_methods"]),
            "kernelOwnedBridgePrefixes": sorted(routing["kernel_owned_bridge_prefixes"]),
            "hostFireableEvents": sorted(routing["host_fireable_events"]),
            "rendererHostCapabilityMethods": sorted(
                routing["renderer_host_capability_methods"]),
            "rendererHostCapabilityEvents": sorted(
                routing["renderer_host_capability_events"]),
            "rendererHostCapabilityDependencies": {
                host: sorted(members)
                for host, members in sorted(
                    routing["renderer_host_capability_dependencies"].items())
            },
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
    "kLegacyMaxDynamicAndSessionRules": "MAX_NUMBER_OF_DYNAMIC_AND_SESSION_RULES",
    "kMaxDynamicRules": "MAX_NUMBER_OF_DYNAMIC_RULES",
    "kMaxSessionRules": "MAX_NUMBER_OF_SESSION_RULES",
    "kMaxUnsafeDynamicRules": "MAX_NUMBER_OF_UNSAFE_DYNAMIC_RULES",
    "kMaxUnsafeSessionRules": "MAX_NUMBER_OF_UNSAFE_SESSION_RULES",
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
    # The current implementation first waits for every target renderer to
    # confirm LoadExtensions/ActivateExtension, then the barrier callback calls
    # ReinjectContentScriptsIntoExistingDocuments. Treat the activation helper
    # as the enable-path call site and separately prove the helper reaches the
    # reinjector; requiring a direct call would regress the renderer race fix.
    reinject_call = enable.find("ActivateExtensionRenderersAndReinject(")
    bump_call = enable.find("BumpActionForExtension(")
    return {
        # 启用路径真的补注入，且落在 state_changed 分支（BumpActionForExtension 只在
        # 那个分支里调；补注入在它之后 = 同分支），避免"宿主重复调启用 → 双重注入"。
        "enable_calls_reinject": (
            reinject_call >= 0 and
            "ReinjectContentScriptsIntoExistingDocuments(" in bridge),
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
        "per_frame_permission": ("CanRunContentScriptOnPage(" in source and
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
            "补注入没有逐帧判权（CanRunContentScriptOnPage / frames_denied）—— 被拒的帧会被"
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
    message_channel = _strip_comments(_read_source(MESSAGE_CHANNEL_SOURCE))
    local_frame_host = _strip_comments(_read_source(LOCAL_FRAME_HOST_SOURCE))
    loader_bridge = _strip_comments(_read_source(LOADER_BRIDGE_SOURCE))
    plugin_jni = _strip_comments(_read_source(PLUGIN_JNI_SOURCE))
    java_bridge = _strip_comments(_read_source(JAVA_BRIDGE_FACADE))
    web_request = _strip_comments(_read_source(WEB_REQUEST_SOURCE))
    intercept_loader = _strip_comments(_read_source(INTERCEPT_LOADER_SOURCE))
    content_browser_client = _strip_comments(
        _read_source(CONTENT_BROWSER_CLIENT_SOURCE))
    ext_api_host = _strip_comments(_read_source(EXT_API_HOST))
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
            "RegisterRetries().erase(worker_context);") >= 2,
        "register_retry_scheduled": (
            "ScheduleRegisterRetry(worker_context, this_attempt);" in sw_host),
        "incognito_context_keyed": (
            "ExtensionWorkerContextKey, int64_t" in sw_service and
            "ExtensionWorkerContextKey, ArupaServiceWorkerHost*" in sw_service),
        "incognito_partition_generation": (
            "extension-background-incognito-" in sw_host and
            "incognito_generation" in sw_host and
            "REMOVE_DATA_MASK_ALL" in sw_host),
        "incognito_access_gate": (
            "IncognitoAllowedExtensions().contains(extension_id)" in sw_host and
            "UnloadExtensionFromIncognitoRenderers(extension_id);" in sw_host and
            "IsExtensionIncognitoAccessAllowed(extension->id())" in loader_bridge),
        "message_routes_by_worker_context": (
            "GetExtensionWorkerStoragePartition(browser_context, worker_context)" in message_channel and
            "CurrentExtensionWorkerContext(target_id, sender_book)" in local_frame_host and
            "worker_context_.book" in sw_service),
        "incognito_host_lease_bridge": (
            "JNI_ArupaPluginBridge_SetExtensionIncognitoSessionActive" in plugin_jni and
            "JNI_ArupaPluginBridge_SetExtensionIncognitoAccess" in plugin_jni and
            "setExtensionIncognitoSessionActive" in java_bridge and
            "setExtensionIncognitoAccess" in java_bridge and
            "getExtensionEventContextForBook" in java_bridge),
        "webrequest_carries_partition": (
            'info.incognito = partition_ == "incognito";' in intercept_loader and
            "MaybeInstallArupaServiceWorkerInterceptProxy" in intercept_loader and
            "URLLoaderFactoryType::kServiceWorkerSubResource" in content_browser_client and
            "MaybeInstallArupaServiceWorkerInterceptProxy(" in content_browser_client and
            "info.incognito ? TabBook::kIncognito : TabBook::kRegular" in web_request),
        "parallel_host_event_contexts": (
            re.search(r"BuildHostEventContextJson\(const std::string\s*&\s*extension_id,\s*"
                      r"bool incognito\)", ext_api_host) is not None and
            "event target context is inactive or unauthorized" in ext_api_host and
            '(*incognito ? "incognito" : "regular")' in ext_api_host and
            "getExtensionEventContextForBook" in java_bridge),
        "java_event_facade_mints_v2_context": (
            "buildExtensionEventContextLocked(id, incognito)" in java_bridge and
            'context.put("source", "android-host")' in java_bridge and
            'context.put("sourceGeneration", sExtEventSourceGeneration)' in java_bridge and
            'context.put("sequence", Long.toString(++sExtEventSequence))' in java_bridge and
            "fireExtEventWithWakeV2(" in java_bridge and
            "fireExtEventV2(" in java_bridge),
        "capability_book_switch_keeps_contexts": (
            "const bool book_only_refresh" in ext_api_host and
            "requires_context_refresh = changed && !book_only_refresh;" in ext_api_host),
        "current_runtime_capability_declaration": (
            "InstallCurrentRuntimeExtApiCapabilities(" in ext_api_host and
            "JNI_ArupaPluginBridge_InstallCurrentRuntimeExtensionHostCapabilities" in plugin_jni and
            "installCurrentRuntimeExtensionHostCapabilities(" in java_bridge and
            'scope.Set("profileId", ProfileIdForThisRun())' in ext_api_host and
            "crypto::SHA256HashString(canonical_json)" in ext_api_host),
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
        "incognito_context_keyed":
            "普通与无痕 Worker 仍只按 extensionId 寻址，两个后台会互相覆盖。",
        "incognito_partition_generation":
            "无痕 Worker 没有会话代次内存分区或结束清理，旧会话状态可能复用。",
        "incognito_access_gate":
            "无痕授权没有同时约束 Renderer 下发、撤权卸载与 Worker 注册。",
        "message_routes_by_worker_context":
            "runtime/tabs 消息没有按调用方册选择对应 Worker 上下文。",
        "incognito_host_lease_bridge":
            "Android 宿主缺少无痕授权与首末租约的同步 JNI 边界。",
        "webrequest_carries_partition":
            "webRequest 未携带真实网络分区，无 tabId 的无痕请求会误投普通后台。",
        "parallel_host_event_contexts":
            "宿主事件上下文仍只能跟随全局无痕状态，不能同时投普通与无痕后台。",
        "java_event_facade_mints_v2_context":
            "Android 事件门面仍调用已禁用的旧入口，通知/书签/后台唤醒会被内核拒绝。",
        "capability_book_switch_keeps_contexts":
            "宿主 capability 仅切换普通/无痕册时仍会销毁另一册上下文。",
        "current_runtime_capability_declaration":
            "Android 宿主能力声明仍要求 Java 自报 profile/scope/digest，或缺少 JNI 安装入口。",
    }
    bad = [n for n, ok in facts.items() if n in failures and not ok]
    if bad:
        err("Service worker 接线不成立：\n"
            + "\n".join(f"  · {failures[n]}" for n in bad))
    return len(failures)


def verify_user_gesture_contract() -> int:
    """Privileged UI APIs must receive the binding's trusted gesture bit."""
    mojom = _read_source(EXT_API_MOJOM)
    ipc = _read_source(EXT_API_IPC)
    host = _read_source(EXT_API_HOST)
    checks = {
        "mojom_carries_gesture":
            "string args_json, bool user_gesture" in mojom,
        "renderer_forwards_gesture":
            ipc.count("args_json, params.user_gesture") >= 2,
        "browser_receives_gesture":
            "const std::string &args_json,\n                           bool user_gesture," in host,
        "permissions_request_requires_gesture":
            'name == "permissions.request"' in host and
            'name + ": requires a user gesture"' in host,
        "action_popup_requires_gesture":
            'name == "action.openPopup"' in host and
            "!user_gesture" in host,
        "side_panel_open_reserved_gate":
            'name == "sidePanel.open"' in host and
            "!user_gesture" in host,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        err("可信 user gesture 接线不完整：\n  " + "\n  ".join(failed))
    return len(checks)


def verify_capability_claims(routing: dict, permissions: dict, schemas: dict,
                             window: dict, wakes: dict, facade: dict) -> dict:
    """声明与实现同源门禁（**双向**）—— 回应 N-C08 / K-A09。

      · 声明有（清单/schema）→ 实现必须有（路由/事件/分发表/常量/门面）
      · 实现有（内核表）      → 声明必须有（schema / 事件白名单）
    任一条不成立就 err() 直接拒绝出包，绝不写一份"看起来通过"的清单。
    """
    # Edge Android browser-side schemas must never exceed the union of kernel
    # and versioned-host routes.
    edge_api = verify_edge_android_api_routes(routing)
    round2 = verify_round2_host_contract(routing)
    product = verify_current_android_product_contract()
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
    gesture_checks = verify_user_gesture_contract()
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
    provider_source = _read_source(
        SRC / ROOT / "plugin/arupa_extensions_api_provider.cc")
    unavailable_protocol_members = set(re.findall(
        r'ARUPA_API_UNAVAILABLE\(\s*"([^"]+)"\s*\)', provider_source))
    for ns, fact in schemas.items():
        if f"{ns}." not in routing["host_owned_prefixes"]:
            err(f"schema 声明了命名空间 {ns}，但路由表没有 {ns}. —— 能声明却调不通。")
        for fn in fact["functions"]:
            qualified = f"{ns}.{fn}"
            if qualified in unavailable_protocol_members:
                continue
            if not _has_prefix(routing["host_owned_prefixes"], qualified) and \
               not _has_prefix(routing["kernel_owned_bridge_prefixes"], qualified):
                err(f"schema 声明了 {qualified}，但内核既不等价转宿主也不桥接 —— 声明与路由不同源。")
        for ev in fact["events"]:
            qualified = f"{ns}.{ev}"
            if qualified in unavailable_protocol_members:
                continue
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
    # 独立审计通知由内核直接派发，保留在监听器表，但不能要求它们出现在
    # 宿主可主动投递白名单；nomad. 前缀不等于 nomadTransfer 权限归属。
    for ev in [e for e in dispatched
               if any(e.startswith(ns + ".") for ns in protos)
               and not e.startswith(AUDIT_EVENT_PREFIXES)]:
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
        f" + Edge Android MV3 命名空间门禁({len(edge_api['edge_namespaces'])} exact)"
        f" + 已声明浏览器路由门禁({len(edge_api['namespaces'])} namespaces / "
        f"{len(edge_api['methods'])} browser methods)"
        f" + R2 宿主合同({round2['methods']} exact methods / "
        f"{round2['events']} events / {round2['checks']} wiring checks)"
        f" + 当前产品合同({product['methods']} methods / {product['events']} events / "
        f"{product['models']} models，{product['formal_families_complete']}/"
        f"{product['formal_families_total']} 个正式族最小集完整，source={product['source']})"
        f" + R-03 补注入({len(kernel_reinject_facts())} 项接线契约全成立："
        f"启用跳变才补 / 代次判活 / 逐帧判权 / 机读回执 / 编译不成环)"
        f" + chrome.storage({len(kernel_storage_facts())} 项接线契约全成立："
        f"session 内存态 / 配额回报 / onChanged 真实写入 / 内部键隔离)"
        f" + SW({len(kernel_sw_facts())} 项：onStartup 每会话一次 / 注册失败有界自愈)"
        f" + user gesture({gesture_checks} 项：renderer→mojom→browser 强制门禁)"
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
