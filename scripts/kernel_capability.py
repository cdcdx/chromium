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
LISTENER_REGISTRY = ROOT / "plugin/arupa_ext_listener_registry.cc"
WORKER_WINDOW_HEADER = ROOT / "plugin/arupa_worker_window.h"
WAKE_OUTBOX_HEADER = ROOT / "plugin/arupa_nomadtransfer_outbox.h"
WAKE_OUTBOX_SOURCE = ROOT / "plugin/arupa_nomadtransfer_outbox.cc"
SW_HOST_SOURCE = ROOT / "plugin/arupa_extension_sw_host.cc"
SELF_TEST_SOURCE = ROOT / "mv3/arupa_mv3_self_test.cc"
JAVA_BRIDGE_FACADE = ROOT / "plugin/android/java/src/co/arupa/plugin/ArupaPluginBridge.java"

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
    bodies = {}
    for m in re.finditer(r"\n[A-Za-z_][\w:<>,\s\*&]*?\b(\w+)\s*\([^;{)]*\)\s*\{", text):
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


def verify_capability_claims(routing: dict, permissions: dict, schemas: dict,
                             window: dict, wakes: dict, facade: dict) -> dict:
    """声明与实现同源门禁（**双向**）—— 回应 N-C08 / K-A09。

      · 声明有（清单/schema）→ 实现必须有（路由/事件/分发表/常量/门面）
      · 实现有（内核表）      → 声明必须有（schema / 事件白名单）
    任一条不成立就 err() 直接拒绝出包，绝不写一份"看起来通过"的清单。
    """
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
        f" + 混淆契约 {keep_count} 条 + 无假保活")
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
                                      MV3_PERMISSION_MAP, LISTENER_REGISTRY,
                                      WORKER_WINDOW_HEADER, WAKE_OUTBOX_HEADER,
                                      WAKE_OUTBOX_SOURCE, SW_HOST_SOURCE,
                                      SELF_TEST_SOURCE, JAVA_BRIDGE_FACADE)]
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
                        "运行期事实入口齐备", "清单窗口键名↔运行期 JSON 键名",
                        "交付件 consumer keep 规则齐备（App 构建会逐条校验）",
                        "无假保活：SW 生命周期只由窗口在持引用驱动"],
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
    ap.add_argument("cmd", nargs="?", choices=("facts", "gate", "manifest"), default="facts")
    ap.add_argument("--stage", help="交付目录（manifest 用；其下应有 kernel/）")
    ap.add_argument("--ver", help="内核版本（manifest 用）")
    ap.add_argument("--num", type=int, help="交付编号（manifest 用）")
    ap.add_argument("--abi-major", type=int, default=1)
    ap.add_argument("--abi-minor", type=int, default=24)
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
    # 说明走 stderr，stdout 只留 JSON（便于 `| python3 -c 'json.load(...)'` 直接用）。
    warn("内核源码事实（只读源码，不需要构建产物）；"
         "用法：python3 scripts/kernel_capability.py [facts|gate|manifest]")
    print(json.dumps(verified_facts(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
