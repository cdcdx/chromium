#!/usr/bin/env python3
"""Export the net changes in the Chromium src repository against a local ref."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from platforms import MATRIX, normalize_os

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = ('out', 'arupa_build', 'chrome/browser/arupa_desktop',
            'chrome/browser/arupa_android', 'chrome/browser/arupa')


def log(message):
    print(f'[INFO] {message}', flush=True)


def git(src, args, *, env=None, data=None, progress=None):
    command = ['git', '--no-optional-locks', '-c', 'core.splitIndex=false', '-C', str(src), *args]
    started = time.monotonic()
    # Own the worker so Ctrl+C also stops a long Git scan before scratch cleanup.
    with subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                          start_new_session=os.name != 'nt') as process:
        try:
            pending = data
            while True:
                try:
                    stdout, stderr = process.communicate(input=pending, timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    pending = None  # communicate retains unsent input and output.
                    if progress:
                        log(f'{progress}，仍在执行（{time.monotonic() - started:.0f} 秒）')
        except BaseException:
            if process.poll() is None:
                if os.name == 'nt':
                    process.terminate()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt':
                        process.kill()
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(stderr.decode('utf-8', errors='replace').strip()
                               or f'Git 执行失败，退出码 {process.returncode}')
        return stdout


def excluded(name):
    return any(name == path or name.startswith(path + '/') for path in EXCLUDED)


def resolve_base(src, ref):
    # fetch uses four-part tags; never silently substitute HEAD when a tag is absent.
    candidate = f'refs/tags/{ref}' if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', ref) else ref
    try:
        return git(src, ['rev-parse', '--verify', '--end-of-options', candidate + '^{commit}']).decode().strip()
    except RuntimeError as exc:
        raise RuntimeError(f'本地无法解析基线 {ref}；请先获取该 tag/commit，或使用已有的完整 commit SHA。\n{exc}') from exc


def collect_patch(src, base, include_untracked):
    """Use an isolated index AND object store; source index/objects stay untouched."""
    if git(src, ['ls-files', '--unmerged', '-z']):
        raise RuntimeError('src 有未解决的合并冲突；请先解决冲突再备份')
    pathspec = ['.', *[':(exclude)' + path for path in EXCLUDED]]
    log('1/4 扫描本地改动（复用现有 Git 索引的文件缓存）')
    status = git(src, ['status', '--porcelain=v1', '-z', '--no-renames',
                       '--ignore-submodules=all',
                       '--untracked-files=all' if include_untracked else '--untracked-files=no',
                       '--', *pathspec], progress='扫描本地改动')
    # With renames disabled each record is exactly "XY path\0". Using status
    # against the real index avoids hashing every Chromium file in a cold index.
    names = {record[3:] for record in status.split(b'\0') if record}
    entries = git(src, ['ls-files', '--stage', '-v', '-z', '--', *pathspec],
                  progress='读取依赖仓库目录')
    repositories = set()
    for record in entries.split(b'\0'):
        if not record:
            continue
        metadata, name = record.split(b'\t', 1)
        if metadata[2:].startswith(b'160000 '):
            repositories.add(name)
        elif metadata[:1].islower() or metadata[:1] == b'S':
            # status intentionally hides assume-unchanged/skip-worktree edits.
            # Inspect those files explicitly; absent sparse files stay in HEAD.
            path = src / os.fsdecode(name)
            if metadata[:1].upper() != b'S' or path.exists() or path.is_symlink():
                names.add(name)
    skipped_dirs = {os.fsdecode(name) for name in repositories}
    additions, removals = [], []
    for raw in sorted(names):
        name = os.fsdecode(raw)
        if excluded(name.rstrip('/')) or raw in repositories:
            continue
        path = src / name
        if path.is_symlink() or path.is_file():
            additions.append(raw)
        elif name.endswith('/'):
            # An untracked embedded repository is reported as a directory.
            skipped_dirs.add(name.rstrip('/'))
        else:
            # Includes deletion and file -> directory transitions. Do not stage
            # the directory recursively: --tracked-only must stay tracked-only.
            removals.append(raw)
    log(f'2/4 收集工作区内容：{len(additions)} 个新增/修改，{len(removals)} 个删除；'
        f'跳过 {len(skipped_dirs)} 个独立仓库目录')
    objects = Path(os.fsdecode(git(src, ['rev-parse', '--git-path', 'objects']).strip()))
    if not objects.is_absolute():
        objects = src / objects
    with tempfile.TemporaryDirectory(prefix='arupa-backup-index-') as tmp:
        scratch = Path(tmp)
        store = scratch / 'objects'
        store.mkdir()
        env = os.environ.copy()
        env.update(GIT_INDEX_FILE=str(scratch / 'index'), GIT_OBJECT_DIRECTORY=str(store),
                   GIT_ALTERNATE_OBJECT_DIRECTORIES=str(objects.resolve()), GIT_OPTIONAL_LOCKS='0')
        git(src, ['read-tree', 'HEAD'], env=env, progress='准备隔离索引')
        if removals:
            git(src, ['update-index', '--force-remove', '-z', '--stdin'], env=env,
                data=b'\0'.join(removals) + b'\0', progress='记录删除文件')
        if additions:
            # Status already filtered genuinely untracked ignored files. Force
            # permits tracked/staged files which now match an ignore rule.
            git(src, ['--literal-pathspecs', 'add', '-f', '--pathspec-from-file=-', '--pathspec-file-nul'],
                env=env, data=b'\0'.join(additions) + b'\0', progress='收集变更文件')
        options = ['diff', '--cached', '--no-ext-diff', '--no-textconv', '--ignore-submodules=all',
                   '--no-renames', base, '--', *pathspec]
        log('3/4 生成相对基线的补丁（包含已提交修改）')
        patch = git(src, [*options[:1], '--binary', '--full-index', '--src-prefix=a/', '--dst-prefix=b/', *options[1:]],
                    env=env, progress='生成二进制补丁')
        changed = git(src, [*options[:1], '--name-status', '-z', *options[1:]], env=env).split(b'\0')
        changes = []
        # --no-renames gives a stable status/path pair; a rename restores as delete+add.
        for i in range(0, len(changed) - 1, 2):
            changes.append({'status': changed[i].decode('ascii'), 'path': os.fsdecode(changed[i + 1])})
        return patch, changes, sorted(skipped_dirs)


def build_parser():
    p = argparse.ArgumentParser(description='将 src 相对指定版本的净修改备份为可恢复的二进制 Git 补丁')
    p.add_argument('--ver', '--base', required=True, dest='base', help='本地 Chromium tag / branch / commit；推荐 tag 或完整 SHA')
    p.add_argument('--os', type=normalize_os, choices=tuple(MATRIX),
                   default={'Windows': 'win', 'Darwin': 'mac', 'Linux': 'linux'}.get(platform.system()),
                   help='备份标记系统，默认宿主；Android 请显式指定')
    p.add_argument('--num', type=int, help='序号，默认按系统和版本自动递增；不覆盖已有备份')
    p.add_argument('--src', type=Path, default=ROOT / 'src', help='源码 Git 仓库（默认工作区 src）')
    p.add_argument('--patches', '--output', type=Path, default=ROOT / 'patches', help='备份根目录（默认工作区 patches）')
    p.add_argument('--tracked-only', action='store_true', help='不包含未跟踪文件；仍包含暂存的新文件')
    p.add_argument('--dry-run', action='store_true', help='只检查基线与目标位置，不生成补丁、不写文件')
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.os or (args.num is not None and args.num < 1):
        parser.error('--os 必须有效，--num 必须为正整数')
    src = args.src.expanduser().resolve()
    destination = args.patches.expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f'patches 输出根路径不是目录: {destination}')
    if not src.is_dir():
        raise RuntimeError(f'源码目录不存在: {src}')
    top = Path(os.fsdecode(git(src, ['rev-parse', '--show-toplevel']).strip())).resolve()
    if top != src:
        raise RuntimeError(f'--src 必须是 Git 仓库根目录，实际根目录: {top}')
    if destination == src or src in destination.parents:
        raise RuntimeError('patches 目录必须在 src 之外，避免把备份自身或源码目录当作输出')
    base = resolve_base(src, args.base)
    head = git(src, ['rev-parse', 'HEAD']).decode().strip()
    label = re.sub(r'[^A-Za-z0-9._-]+', '_', args.base)[:80].strip('.') or base[:12]
    prefix = f'src-{args.os}-{label}-'
    def next_number():
        pattern = re.compile(re.escape(prefix) + r'(\d+)\.patches(?:\.lock)?$')
        numbers = [int(m[1]) for path in destination.iterdir()
                   if (m := pattern.fullmatch(path.name))] if destination.is_dir() else []
        return args.num if args.num is not None else max(numbers, default=0) + 1
    number = next_number()
    final = destination / f'{prefix}{number}.patches'
    if final.exists() or final.is_symlink():
        raise RuntimeError(f'备份已存在，不会覆盖: {final}')
    if args.num is not None and (destination / (final.name + '.lock')).exists():
        raise RuntimeError(f'备份序号正在使用: {final}')
    if args.dry_run:
        print(f'[INFO] 基线 {args.base} -> {base}\n[INFO] HEAD {head}\n[INFO] 输出目录 {final}\n[INFO] dry-run：未生成补丁或写入文件')
        return 0
    started = time.monotonic()
    log(f'开始备份：基线 {args.base} ({base[:12]})，HEAD {head[:12]}')
    log(f'输出根目录：{destination}')
    patch, changes, skipped = collect_patch(src, base, not args.tracked_only)
    # Reject a moving checkout, which could otherwise claim the wrong HEAD.
    if git(src, ['rev-parse', 'HEAD']).decode().strip() != head:
        raise RuntimeError('备份过程中 HEAD 发生变化，请停止其他 Git 操作后重试')
    timestamp = datetime.now(timezone.utc)
    manifest = {
        'format': 'arupa-src-backup/1', 'created_at': timestamp.isoformat(),
        'os': args.os, 'version_label': label,
        'source': str(src), 'base_ref': args.base, 'base_commit': base, 'head_commit': head,
        'include_untracked': not args.tracked_only, 'excluded_paths': list(EXCLUDED),
        'scope': 'src repository only; nested repositories/submodules and ignored untracked files excluded',
        'skipped_repository_directories': skipped, 'changes': changes,
        'patch': 'chromium.patch', 'patch_sha256': hashlib.sha256(patch).hexdigest(),
    }
    destination.mkdir(parents=True, exist_ok=True)
    # Reserve a sequence using an exclusive sibling lock. Concurrent invocations
    # retry the next number; explicit --num always fails rather than overwriting.
    while True:
        number = next_number()
        final = destination / f'{prefix}{number}.patches'
        lock = destination / (final.name + '.lock')
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if args.num is not None:
                raise RuntimeError(f'备份序号正在使用: {final}')
            continue
        os.close(handle)
        if final.exists() or final.is_symlink():
            lock.unlink()
            if args.num is not None:
                raise RuntimeError(f'备份已存在，不会覆盖: {final}')
            continue
        break
    stage = None
    try:
        log(f'4/4 写入备份：{len(changes)} 个文件变更，补丁 {len(patch)} 字节')
        stage = Path(tempfile.mkdtemp(prefix='.backup-', dir=destination))
        manifest['number'] = number
        (stage / 'chromium.patch').write_bytes(patch)
        (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        restore = (f'# src 修改备份\n\n基线：`{base}`\n\n当前 HEAD：`{head}`\n\n'
                   '包含相对基线的最终工作区内容，不能恢复原来的暂存/未暂存划分。\n'
                   '重命名使用删除+新增表示；二进制、符号链接和 Git 文件模式保留。\n'
                   '不包含依赖仓库的内部修改、忽略的新文件、构建产物或 Arupa 独立仓库。\n\n'
                   '在另一份干净检出中恢复，先检出基线，再使用本备份的绝对补丁路径：\n\n'
                   f'```sh\ngit checkout --detach {base}\n'
                   'git apply --check "/path/to/backup/chromium.patch"\n'
                   'git apply --binary "/path/to/backup/chromium.patch"\n```\n\n'
                   '若 changes 为空且补丁为空，无需执行 git apply。备份期间请勿并发修改源码。\n')
        (stage / 'README.md').write_text(restore, encoding='utf-8')
        stage.rename(final)
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        lock.unlink()
    log(f'备份完成: {final}')
    log(f'{len(changes)} 个文件变更，补丁 {len(patch)} 字节，耗时 {time.monotonic() - started:.1f} 秒')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
