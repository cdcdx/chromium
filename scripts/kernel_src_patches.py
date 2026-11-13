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
    两边都不通          → 逐文件复核（见下）

为什么冲突时要逐文件复核（2026-11-11 实证）
------------------------------------------
补丁被重写时（`chromium-devtools-external-dock-154` 从"改 emulation-meta.ts"
改成"改 AdvancedApp.ts + DeviceModeModel.ts"），旧版应用结果会留在工作区：
两版共有的文件停在旧后像、新加的文件还是前像 —— 整补丁正向/反向试打同时失败，
报出来的却是"基线不符"，误导成要改补丁。更隐蔽的是 devtools-frontend 是**独立
git 仓库**（gitlink + `submodule.<path>.ignore=dirty`），外层 `git -C src status`
恒显示 clean，残留完全看不见。

所以冲突分支按 `diff --git` 拆成单文件再判定：
    全部前像              → 正常应用
    前像/后像混合，无第三态 → 旧版残留：反向还原后像文件，再重打整补丁（自愈）
    出现第三态            → 真的基线不符：列出文件与所在子仓的脏改动，交由人处理

改补丁的流程：先 `--restore` 还原，再改 src 验证，最后重新导出 patch。
注意 `--restore` 只能按**当前补丁文本**反推，所以顺序不能反：补丁文件一旦被重写成
不再包含某个旧文件，那个旧文件上的残留就无法由它发现（2026-11-11 的
`emulation-meta.ts` 就是这样漏掉的）。

用法：`scripts/build.py` 在 arupa_desktop / arupa_android 的 gen 之前调用
`ensure_applied(SRC, ROOT / project / "patches", dry_run=...)`（GN 会核对 BUILD.gn 里列出的
源文件是否存在，所以必须在 gen 之前）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

PATCH_SUFFIX = ".patch"

_DIFF_LINE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")

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


def probe_file(src, chunk: str) -> str:
    """单文件补丁的三态；只用在整补丁 conflict 之后的归因。"""
    applied, _ = _git_apply_check(src, chunk, reverse=True)
    if applied:
        return "applied"
    pending, _ = _git_apply_check(src, chunk, reverse=False)
    return "pending" if pending else "conflict"


def split_patch_files(text: str) -> list:
    """按 `diff --git` 拆成 [(路径, 单文件补丁)]，顺序与整补丁一致。"""
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", text)]
    chunks = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        chunk = text[start:end]
        header = _DIFF_LINE.match(chunk.split("\n", 1)[0])
        chunks.append((header.group("b") if header else "<unknown>", chunk))
    return chunks


def _owning_repo(src, path: str):
    """包含该路径的最内层 git 仓库（src 本身或它内部的独立检出）；没有则 None。"""
    root = Path(src).resolve()
    current = (root / path).parent
    while True:
        if (current / ".git").exists():
            return current
        if current == root or root not in current.parents:
            return None
        current = current.parent


def _nested_repo(src, path: str):
    """只在独立检出（如 devtools-frontend）里返回仓库：外层 src 的脏状态本就能直接看到。"""
    repo = _owning_repo(src, path)
    return None if repo is None or repo == Path(src).resolve() else repo


def _is_committed(src, path: str) -> bool:
    """文件相对 HEAD 是否有未提交改动（含暂存区与未跟踪）。"""
    repo = _owning_repo(src, path)
    if repo is None:
        return False
    try:
        relative = (Path(src).resolve() / path).resolve().relative_to(repo)
    except ValueError:
        return False
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(relative)],
        capture_output=True)
    if tracked.returncode != 0:
        return False           # 未跟踪（补丁新增的文件还没 git add）→ 未提交
    for extra in ([], ["--cached"]):
        completed = subprocess.run(
            ["git", "-C", str(repo), "diff", "--quiet"] + extra + ["--", str(relative)],
            capture_output=True)
        if completed.returncode != 0:
            return False
    return True


def _dirty_lines(repo, source) -> list:
    completed = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True)
    if completed.returncode != 0:
        return []
    dirty = [line for line in completed.stdout.decode("utf-8", errors="replace").splitlines()
             if line.strip()]
    if not dirty:
        return []
    try:
        label = str(Path(repo).resolve().relative_to(Path(source).resolve()))
    except ValueError:
        label = str(repo)
    shown = dirty[:5]
    lines = [f"      {label} 子仓未提交改动（{len(dirty)} 项）:"]
    lines.extend(f"        {line}" for line in shown)
    if len(dirty) > len(shown):
        lines.append(f"        … 另有 {len(dirty) - len(shown)} 项")
    return lines


def conflict_message(source, patches_dir, patch_path, parts) -> str:
    """把"打不上"翻译成可执行的结论：哪些文件停在哪一态、哪个子仓是脏的。"""
    lines = [
        f"内核补丁 {patch_path.name} 既打不上、也不处于已应用状态：src 基线与该补丁不匹配。",
        f"  · 补丁: {patch_path}",
        f"  · 目标: {source}",
    ]
    if parts:
        lines.append("  · 逐文件状态:")
        lines.extend(f"      {STATES.get(state, state)}  {path}" for path, _, state in parts)
        reported = set()
        for path, _, state in parts:
            if state != "conflict":
                continue
            repo = _nested_repo(source, path)
            if repo is None or repo in reported:
                continue
            reported.add(repo)
            lines.extend(_dirty_lines(repo, source))
    lines.append(
        "  · 处理: 若补丁刚被重写、旧版结果还留在工作区，先还原再构建："
        f"\n      python3 scripts/kernel_src_patches.py --src {source}"
        f" --patches {patches_dir} --restore"
        "\n    还原后仍打不上，才说明需要内核仓对齐补丁基线。")
    return "\n".join(lines)


def apply_patch(src, text: str, reverse: bool = False) -> None:
    command = ["git", "apply"] + (["--reverse"] if reverse else []) + ["-"]
    completed = subprocess.run(command, cwd=str(src),
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
            parts = [(file, chunk, probe_file(source, chunk))
                     for file, chunk in split_patch_files(text)]
            residue = [file for file, _, file_state in parts if file_state == "applied"]
            dirty_residue = [file for file in residue if not _is_committed(source, file)]
            clean = all(file_state in ("applied", "pending") for _, _, file_state in parts)
            if parts and clean and dirty_residue and len(dirty_residue) == len(residue):
                # 旧版补丁未提交的后像还留在工作区：这些文件只可能因补丁而变，
                # 反向还原到前像再重打整补丁，比让人工清理可靠。
                # 已提交的后像不在此列 —— 那是分支基线的一部分，反向应用只会弄脏工作区。
                if dry_run:
                    say(f"(dry-run) 检测到旧版补丁残留，正式运行会先还原再重打: "
                        f"{', '.join(dirty_residue)}")
                    state = "pending"
                else:
                    for _, chunk, file_state in parts:
                        if file_state == "applied":
                            apply_patch(source, chunk, reverse=True)
                    say(f"已还原旧版补丁残留: {', '.join(residue)}")
                    if probe(source, text) != "pending":
                        raise PatchError(
                            conflict_message(source, patches_dir, path, parts))
                    try:
                        apply_patch(source, text)
                    except PatchError as error:
                        raise PatchError(
                            f"内核补丁 {path.name} 还原残留后仍打不上: {error}\n"
                            f"  · 补丁: {path}\n"
                            f"  · 目标: {source}") from error
                    say(f"已应用内核补丁: {path.name}（源: {path.parent}）")
                    state = "applied-now"
            else:
                raise PatchError(conflict_message(source, patches_dir, path, parts))
        results.append((path.name, state))
    return results


def restore_patches(src, patches_dir,
                    logger: Optional[Callable[[str], None]] = None) -> list:
    """反向应用已应用补丁，把 src 还原成补丁前基线。

    改补丁前必须先跑这一步：否则旧版结果会和新补丁混在一起，变成
    "既打不上、也不处于已应用状态"的半应用态（见模块头）。
    已经是 src 提交的一部分的改动不还原 —— 反向应用只会把干净的树弄脏。
    """
    say = logger or log
    source = Path(src)
    results = []
    for path in patch_files(patches_dir):
        text = read_patch(path)
        parts = [(file, chunk, probe_file(source, chunk))
                 for file, chunk in split_patch_files(text)]
        if not parts:
            raise PatchError(conflict_message(source, patches_dir, path, parts))
        if any(file_state == "conflict" for _, _, file_state in parts):
            raise PatchError(conflict_message(source, patches_dir, path, parts))
        applied = [(file, chunk) for file, chunk, file_state in parts
                   if file_state == "applied"]
        if not applied:
            say(f"补丁本未应用: {path.name}")
            results.append((path.name, "pending"))
            continue
        revert = [file for file, chunk in applied if not _is_committed(source, file)]
        kept = [file for file, chunk in applied if _is_committed(source, file)]
        for file, chunk in applied:
            if file in revert:
                apply_patch(source, chunk, reverse=True)
        if revert:
            say(f"已还原补丁: {path.name}（{', '.join(revert)}）")
        if kept:
            say(f"跳过已提交改动: {path.name}（{', '.join(kept)}）；"
                "它们属于 src 分支本身，需要时用 git 操作")
        results.append((path.name, "reverted" if revert else "committed"))
    return results


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="应用内核仓的 Chromium 侧补丁（幂等）")
    parser.add_argument("--src", required=True, help="Chromium 源码树（通常是 <root>/src）")
    parser.add_argument("--patches", required=True, help="补丁目录（通常是 <root>/arupa_desktop/patches）")
    parser.add_argument("--dry-run", action="store_true", help="只报告状态，不落盘")
    parser.add_argument("--restore", action="store_true",
                        help="反向应用已应用补丁（改补丁前先还原，避免旧版残留）")
    args = parser.parse_args(argv)
    try:
        if args.restore:
            results = restore_patches(args.src, args.patches)
            if not results:
                log(f"没有内核补丁需要还原: {args.patches}")
            return 0
        results = ensure_applied(args.src, args.patches, dry_run=args.dry_run)
    except PatchError as error:
        print(f"[ERROR] {error}", file=sys.stderr, flush=True)
        return 1
    if not results:
        log(f"没有内核补丁需要处理: {args.patches}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
