#!/usr/bin/env python3
"""把内核仓自带的 Chromium 侧补丁应用进 src（幂等；打不上就明确失败）。

背景（2026-10-12，Windows 内核构建实证）
--------------------------------------
内核第十一批（ABI 1.36 / `dnr.execution.v3` / WebSocket 上下文）要求 src 侧**同时**打一个
Chromium 补丁：`arupa_desktop/patches/chromium-websocket-context-154.patch` 新增
`content/public/browser/websocket_connect_context.h`，并在 `websocket_connector_impl.cc`
的 Connect 路径上挂出 `WebSocketConnectContext`。补丁只躺在内核仓里，而构建流程从不应用它，
症状是：

    ../../chrome/browser/arupa_desktop/app/arupa_intercept_loader.cc(11,10):
    fatal error: 'content/public/browser/websocket_connect_context.h' file not found

看着像内核源码写错了，其实是"少打了一个 src 侧补丁"。

Windows 专属坑：内核仓 `core.autocrlf=true`（本机实测），检出的 `.patch` 是 CRLF，而
Chromium 源码是 LF，`git apply` 会以 "patch does not apply" 拒绝**全部** hunk —— 于是
"补丁明明在、也明明没打上"两个事实同时成立。所以这里一律把补丁文本规范成 LF 再喂给
`git apply`，并且读**工作区文件**（内核仓里未提交的补丁改动也算数），不走 `git show`。

判据（每个补丁三态，先反向再正向）
----------------------------------
    反向 `--check` 通过 → 已应用（幂等，不重复打）
    正向 `--check` 通过 → 未应用 → 应用
    两边都不通          → 基线不符（补丁是对着别的 Chromium 修订写的）→ 明确失败

用法：`scripts/build.py` 在 arupa_desktop / arupa_android 的 gen 之前调用
`ensure_applied(SRC, ROOT / project / "patches", dry_run=...)`（GN 会核对 BUILD.gn 里列出的
源文件是否存在，所以必须在 gen 之前）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

PATCH_SUFFIX = ".patch"

STATES = {
    "applied": "已应用",
    "applied-now": "本次应用",
    "pending": "待应用",
    "conflict": "基线不符",
}


class PatchError(RuntimeError):
    pass


def log(message):
    print(f"[INFO] {message}", file=sys.stderr, flush=True)


def normalize(text: str) -> str:
    """统一成 LF：CRLF / 孤立 CR 都还原，避免 autocrlf 把 .patch 变成"打不上的补丁"。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_patch(path) -> str:
    return normalize(Path(path).read_bytes().decode("utf-8", errors="replace"))


def patch_files(patches_dir) -> list:
    """补丁目录里的 *.patch，按文件名排序（顺序稳定 = 结果可复现）。"""
    directory = Path(patches_dir)
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.iterdir()
                   if path.is_file() and path.name.endswith(PATCH_SUFFIX)),
                  key=lambda path: path.name)


def _git_apply_check(src, text: str, reverse: bool = False) -> tuple:
    """git apply --check（补丁走 stdin）。返回 (是否通过, 原始输出)。"""
    command = ["git", "apply", "--check"] + (["--reverse"] if reverse else []) + ["-"]
    completed = subprocess.run(command, cwd=str(src), input=text.encode("utf-8"),
                              capture_output=True)
    output = (completed.stderr or b"").decode("utf-8", errors="replace")
    return completed.returncode == 0, output


def probe(src, text: str) -> str:
    """applied / pending / conflict —— 见模块头三态表。"""
    applied, _ = _git_apply_check(src, text, reverse=True)
    if applied:
        return "applied"
    pending, _ = _git_apply_check(src, text, reverse=False)
    return "pending" if pending else "conflict"


def apply_patch(src, text: str) -> None:
    completed = subprocess.run(["git", "apply", "-"], cwd=str(src),
                              input=text.encode("utf-8"), capture_output=True)
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise PatchError(detail or "git apply 失败")


def ensure_applied(src, patches_dir, dry_run: bool = False,
                   logger: Optional[Callable[[str], None]] = None) -> list:
    """返回 [(补丁名, 状态)]。冲突即抛 PatchError（调用方转成构建错误）。"""
    say = logger or log
    source = Path(src)
    patches = patch_files(patches_dir)
    if not patches:
        return []                      # 没有补丁就不碰源码树（也不要求它有 git/src）
    if not source.is_dir():
        raise PatchError(f"源码目录不存在: {source}")
    if shutil.which("git") is None:
        raise PatchError("找不到 git；内核仓的 Chromium 侧补丁无法应用")

    results = []
    for path in patches:
        text = read_patch(path)
        state = probe(source, text)
        if state == "applied":
            say(f"内核补丁已应用: {path.name}")
        elif state == "pending":
            if dry_run:
                say(f"(dry-run) 待应用内核补丁: {path.name}")
                state = "pending"
            else:
                try:
                    apply_patch(source, text)
                except PatchError as error:
                    raise PatchError(
                        f"内核补丁 {path.name} 打不上（它是对着另一份 Chromium 修订写的）: {error}\n"
                        f"  · 补丁: {path}\n"
                        f"  · 目标: {source}\n"
                        "  · 处理: 对齐 Chromium 基线后由内核仓更新该补丁；"
                        "不要手工改 src（下一次 fetch/还原会丢）") from error
                say(f"已应用内核补丁: {path.name}（源: {path.parent}）")
                state = "applied-now"
        else:
            raise PatchError(
                f"内核补丁 {path.name} 既打不上、也不处于已应用状态：src 基线与该补丁不匹配。\n"
                f"  · 补丁: {path}\n"
                f"  · 目标: {source}\n"
                "  · 处理: 先用 git -C src status / diff 看清 src 现状，再由内核仓对齐补丁基线")
        results.append((path.name, state))
    return results


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="应用内核仓的 Chromium 侧补丁（幂等）")
    parser.add_argument("--src", required=True, help="Chromium 源码树（通常是 <root>/src）")
    parser.add_argument("--patches", required=True, help="补丁目录（通常是 <root>/arupa_desktop/patches）")
    parser.add_argument("--dry-run", action="store_true", help="只报告状态，不落盘")
    args = parser.parse_args(argv)
    try:
        results = ensure_applied(args.src, args.patches, dry_run=args.dry_run)
    except PatchError as error:
        print(f"[ERROR] {error}", file=sys.stderr, flush=True)
        return 1
    if not results:
        log(f"没有内核补丁需要处理: {args.patches}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
