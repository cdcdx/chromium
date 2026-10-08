"""交付目录的公共约定：`-<序号>` 命名、残留清理、旧交付裁剪。

内核交付（`scripts/packaging/package-arupa_*.sh` → `dist/arupa-<os>-<arch>-<ver>-static-<n>/`，
`build.py` 调起）与浏览器交付（`scripts/nomad.py` → `dist/nomad-<os>-<arch>-<ver>-<variant>-<n>/`）
都按序号递增，也都会在删除受限的环境里改用"改名搁置"（`save_output` / package 的收尾），
于是 out/ 与 dist/ 会堆下整份拷贝（每个近 800MB）。

这里收口命名匹配、残留清理与旧交付裁剪：两条打包链路共用一份实现，删除相关的规则不漂移。
所有删除都在独立子进程会话里执行，被删除闸门/权限拒绝时只返回 False，由调用方打印警告，
不会牵连打包本身。
"""
from __future__ import annotations

from pathlib import Path
import re
import shlex
import subprocess
import sys

import fetch as F

# 脚本自己产出的残留命名：改名搁置（`<name>.trash-<hex>`）与暂存目录（`.publish-*` 等）。
STALE_PREFIXES = ('.publish-', '.browser-package-', '.arupa-package-')
STALE_SUFFIX = re.compile(r'\.trash-[0-9a-f]{4,}$')
DELETE_CODE = ('import os, shutil, sys\n'
               'target = sys.argv[1]\n'
               'if os.path.isdir(target) and not os.path.islink(target):\n'
               '    shutil.rmtree(target)\n'
               'else:\n'
               '    os.unlink(target)\n')


def remove_path(path):
    """删除目录/文件，交给独立会话的子进程执行。

    受限环境的删除闸门会拒绝（乃至终止）批量删除：放在子进程里，被拒绝只影响这一次清理，
    不会连带把打包本身带走；父进程按返回码决定是否提示手工清理。"""
    try:
        return subprocess.run([sys.executable, '-c', DELETE_CODE, str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True).returncode == 0
    except OSError:
        return False


def clean_stale_directories(*roots):
    """清理构建/打包旁置的残留（`*.trash-*`、`.publish-*`、`.browser-package-*`）。

    正常路径不留东西；一旦打包中断或反复重建，out/ 与 dist/ 就会堆下整份拷贝。只按自己产出的
    命名规则匹配，不碰交付目录、构建目录和内核交付，返回清理掉的项数。"""
    removed, refused = 0, []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not (STALE_SUFFIX.search(entry.name) or entry.name.startswith(STALE_PREFIXES)):
                continue
            if remove_path(entry):
                removed += 1
            else:
                refused.append(entry)
    if removed:
        F.log(f'清理残留目录/文件: {removed} 项')
    if refused:
        F.log(f'警告: {len(refused)} 项残留未能清理（删除被拒绝或权限不足）；可在普通终端执行: '
              'rm -rf ' + ' '.join(shlex.quote(str(path)) for path in refused))
    return removed


def numbers(destination, prefix):
    """列出交付根里同前缀（os/arch/版本/variant）的已有序号，倒序。"""
    pattern = re.compile(re.escape(prefix) + r'(\d+)$')
    return sorted((int(m[1]) for p in destination.iterdir() if (m := pattern.fullmatch(p.name))),
                  reverse=True)


def newest_number(destination, prefix):
    """最新序号；没有任何交付时返回 0。打包成功后用它定位刚产出的那一份。"""
    existing = numbers(destination, prefix)
    return existing[0] if existing else 0


def prune(destination, prefix, keep, created=None):
    """删除被取代的旧交付，同身份只保留最近 keep 份（含本次）。

    keep = 0 表示保留全部历史、只清残留。只按同一前缀的 `-<序号>` 匹配，同目录里的内核交付
    （arupa-*）与其它 variant 都不受影响。created 为本次产出的序号（缺省取最新序号）。"""
    if keep <= 0:
        return
    superseded = numbers(destination, prefix)
    created = newest_number(destination, prefix) if created is None else created
    # 本次刚产出的那份已占一个名额，剩下的按序号从大到小留 keep-1 份。
    for stale in [n for n in superseded if n != created][keep - 1:]:
        base = destination / f'{prefix}{stale}'
        for path in (base, Path(str(base) + '.zip')):
            if not path.exists() and not path.is_symlink():
                continue
            if remove_path(path):
                F.log(f'清理过期交付: {path.name}')
            else:
                F.log(f'警告: 过期交付未能清理（删除被拒绝或权限不足）；可在普通终端执行: '
                      f'rm -rf {shlex.quote(str(path))}')
