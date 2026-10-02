"""Read-only Git inventory of the caller's immediate child directories."""
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
