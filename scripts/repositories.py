"""Git inventory and current-branch synchronization of immediate child directories."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


def repositories(root, onerror):
    """One level only, including worktrees/bare repos but excluding symlink aliases."""
    try:
        with os.scandir(root) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
    except OSError as error:
        onerror(error)
        return
    for entry in children:
        try:
            if entry.name == '.git' or not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            marker = path / '.git'
            if marker.is_dir() or marker.is_file():
                yield path
            elif ((path / 'HEAD').is_file() and (path / 'config').is_file()
                  and (path / 'objects').is_dir() and (path / 'refs').is_dir()):
                yield path
        except OSError as error:
            onerror(error)


def git(path, *arguments):
    return subprocess.run(['git', '--no-optional-locks', '--no-pager', '-C', str(path), *arguments],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


def sync_branch(path, action):
    def checked(*arguments):
        result = git(path, *arguments)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Git 命令失败')
        return result.stdout.strip()
    if checked('rev-parse', '--is-bare-repository') == 'true':
        raise RuntimeError('裸仓库没有工作分支，不执行同步')
    branch = checked('symbolic-ref', '--quiet', '--short', 'HEAD')
    checked('rev-parse', '--verify', 'HEAD')
    remote = checked('config', '--get', f'branch.{branch}.remote')
    refs = checked('config', '--get-all', f'branch.{branch}.merge').splitlines()
    if len(refs) != 1 or not refs[0].startswith('refs/heads/'):
        raise RuntimeError('当前分支必须配置唯一的远端分支 upstream')
    if remote == '.' or remote.startswith('-'):
        raise RuntimeError('当前分支未配置有效的远端 upstream')
    if action == 'pull' and checked('status', '--porcelain'):
        raise RuntimeError('工作区有未提交或未跟踪修改，请先处理后再 pull')
    ref = refs[0]
    print(f'[INFO] {branch} -> {remote}/{ref.removeprefix("refs/heads/")}', flush=True)
    if action == 'pull':
        # Do not let user configuration enable automatic rebase, merge, or stash.
        return checked('pull', '--ff-only', '--no-rebase', '--no-autostash', remote, ref)
    # Explicit refspec avoids push.default=matching and remote push mappings.
    return checked('-c', 'push.followTags=false', '-c', f'remote.{remote}.mirror=false',
                   'push', remote, f'HEAD:{ref}')


def sync(root, action, dry_run=False):
    root = root.resolve()
    errors = []
    count = success = 0
    def scan_error(error):
        errors.append(str(error))
        print(f'[ERROR] 无法扫描: {error}')
    print(f'[INFO] 扫描一级子目录: {root}')
    for path in repositories(root, scan_error):
        count += 1
        print(f'\n=== {path.name} ===', flush=True)
        if dry_run:
            print(f'[INFO] (dry-run) {action} 当前分支 upstream（执行时校验分支和配置）')
            continue
        try:
            output = sync_branch(path, action)
            print(output or f'[INFO] {action} 成功')
            success += 1
        except (RuntimeError, OSError) as error:
            errors.append(f'{path.name}: {error}')
            print(f'[ERROR] {error}；需处于有提交的本地分支并配置 upstream')
    print(f'\n[INFO] 共 {count} 个仓库，成功 {success} 个，错误 {len(errors)} 个'
          + ('（dry-run，未执行）' if dry_run else ''))
    return 1 if errors else 0


def show(root, actions, dry_run=False):
    root = root.resolve()
    errors = []
    count = 0
    def scan_error(error):
        errors.append(str(error))
        print(f'[ERROR] 无法扫描: {error}')
    print(f'[INFO] 扫描一级子目录: {root}')
    for path in repositories(root, scan_error):
        count += 1
        label = path.relative_to(root).as_posix()
        print(f'\n=== {label} ===', flush=True)
        for action in actions:
            if dry_run:
                print(f'[INFO] (dry-run) git {"log -1" if action == "log" else "branch -vv"}')
                continue
            print(f'[{action}]')
            if action == 'log':
                result = git(path, 'log', '-1', '--no-color', '--no-show-signature', '--date=iso-strict',
                             '--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n    %s')
            else:
                result = git(path, 'branch', '-vv', '--no-color')
            if result.returncode:
                # Unborn branches are valid empty repositories, not failed fetches.
                if action == 'log' and git(path, 'symbolic-ref', '--quiet', 'HEAD').returncode == 0 and git(path, 'rev-parse', '--verify', 'HEAD').returncode != 0:
                    print('(尚无提交)')
                    continue
                message = result.stderr.strip() or result.stdout.strip()
                errors.append(f'{label}: {message}')
                print(f'[ERROR] {message}')
                continue
            print(result.stdout.rstrip() or '(尚无分支)')
    print(f'\n[INFO] 共 {count} 个仓库，{len(errors)} 个错误')
    return 1 if errors else 0
