"""Shared Xcode selection and download-free SDK/Metal validation."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

import fetch as F


def xcode_directory(cfg):
    configured = F.cget(cfg, "chromium_developer_dir") or os.environ.get("DEVELOPER_DIR", "")
    if F.DRY_RUN:
        developer = Path(configured).expanduser() if configured else Path("/Applications/Xcode.app/Contents/Developer")
        return developer / 'Contents/Developer' if developer.suffix == '.app' else developer
    if configured:
        developer = Path(configured).expanduser()
    else:
        selected = subprocess.run(['xcode-select', '-p'], capture_output=True, text=True)
        developer = Path(selected.stdout.strip()) if selected.returncode == 0 else Path('/nonexistent')
        if not (developer / 'usr/bin/xcodebuild').is_file():
            candidates = sorted({p / 'Contents/Developer'
                                 for base in (Path('/Applications'), Path.home() / 'Applications')
                                 for p in base.glob('Xcode*.app')
                                 if (p / 'Contents/Developer/usr/bin/xcodebuild').is_file()})
            if len(candidates) > 1:
                F.err('发现多个 Xcode，请在 .env 中设置 chromium_developer_dir: ' + ', '.join(map(str, candidates)))
            if candidates:
                developer = candidates[0]
    if developer.suffix == '.app':
        developer = developer / 'Contents/Developer'
    if not (developer / 'usr/bin/xcodebuild').is_file():
        F.err(f'未找到完整 Xcode（当前路径: {developer}）。Command Line Tools 不能用于此构建。'
              '请安装完整 Xcode，并在 .env 设置 chromium_developer_dir=/Applications/Xcode.app/Contents/Developer'
              '（按实际安装位置修改）。')
    return developer


def prepare_mac_toolchain(cfg):
    developer = xcode_directory(cfg)
    if F.DRY_RUN:
        F.log(f"(dry-run) 检查完整 Xcode: {developer}")
        return
    environment = dict(os.environ, DEVELOPER_DIR=str(developer))
    for command in (['/usr/bin/xcodebuild', '-version'], ['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path']):
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        if result.returncode:
            F.err(f'Xcode 预检查失败: {" ".join(command)}\n{result.stderr.strip() or result.stdout.strip()}')
    # xcrun --find can resolve a shim even when the downloadable toolchain is absent.
    metal = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', 'metal', '--version'],
                           capture_output=True, text=True, env=environment)
    if metal.returncode:
        install = shlex.join(['env', f'DEVELOPER_DIR={developer}', '/usr/bin/xcodebuild',
                              '-downloadComponent', 'MetalToolchain'])
        F.err(f'Metal 编译器不可用，请先安装当前 Xcode 的 Metal Toolchain：\n{install}\n'
              f'{metal.stderr.strip() or metal.stdout.strip()}')
    os.environ['DEVELOPER_DIR'] = str(developer)
    F.log(f'Xcode: {developer}')
