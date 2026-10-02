"""Explicit toolchain preparation; build remains download-free."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

import fetch as F

TARGETS = {'metal', 'dotnet', 'android-sdk', 'jdk'}


def local_dotnet():
    return F.WORKSPACE_ROOT / '.tools/dotnet' / ('dotnet.exe' if F.IS_WIN else 'dotnet')


def dotnet_executable(cfg):
    return F.cget(cfg, 'dotnet_path') or (str(local_dotnet()) if local_dotnet().is_file() else 'dotnet')


def android_paths(cfg):
    sdk = F.cget(cfg, 'android_home', 'android_sdk_root')
    jdk = F.cget(cfg, 'java_home')
    return (Path(sdk).expanduser() if sdk else F.CHROMIUM_SRC / 'third_party/android_sdk/public',
            Path(jdk).expanduser() if jdk else F.CHROMIUM_SRC / 'third_party/jdk/current')


def xcode_directory(cfg):
    configured = F.cget(cfg, 'chromium_developer_dir') or os.environ.get('DEVELOPER_DIR')
    if configured:
        path = Path(configured).expanduser()
        return path / 'Contents/Developer' if path.suffix == '.app' else path
    if F.DRY_RUN:
        return Path('/Applications/Xcode.app/Contents/Developer')
    selected = subprocess.run(['xcode-select', '-p'], capture_output=True, text=True)
    if selected.returncode == 0:
        path = Path(selected.stdout.strip())
        if (path / 'usr/bin/xcodebuild').is_file():
            return path
    candidates = {p / 'Contents/Developer' for base in (Path('/Applications'), Path.home() / 'Applications')
                  for p in base.glob('Xcode*.app') if (p / 'Contents/Developer/usr/bin/xcodebuild').is_file()}
    if len(candidates) == 1:
        return candidates.pop()
    F.err('请先安装完整 Xcode；多个版本时设置 chromium_developer_dir。Command Line Tools 不够。')


def setup_metal(cfg):
    if F.HOST_OS != 'mac':
        F.err('Metal Toolchain 只能在 macOS 安装')
    developer = xcode_directory(cfg)
    if not F.DRY_RUN:
        if not (developer / 'usr/bin/xcodebuild').is_file():
            F.err(f'完整 Xcode 不存在: {developer}')
        os.environ['DEVELOPER_DIR'] = str(developer)
        check = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', 'metal', '--version'],
                               capture_output=True, text=True)
        if check.returncode == 0:
            F.log('Metal Toolchain 已可用')
            return
    F.run(['env', f'DEVELOPER_DIR={developer}', '/usr/bin/xcodebuild', '-downloadComponent', 'MetalToolchain'])
    F.run(['env', f'DEVELOPER_DIR={developer}', '/usr/bin/xcrun', '--kill-cache'])
    F.run(['env', f'DEVELOPER_DIR={developer}', '/usr/bin/xcrun', '--sdk', 'macosx', 'metal', '--version'])


def setup_dotnet(cfg):
    repo = F.WORKSPACE_ROOT / 'nomadbrowser.pc'
    pin = repo / 'global.json'
    version = F.cget(cfg, 'dotnet_version')
    if not version and pin.is_file():
        version = json.loads(pin.read_text(encoding='utf-8-sig')).get('sdk', {}).get('version', '')
    channel = F.cget(cfg, 'dotnet_channel', default='10.0')
    executable = os.path.expanduser(dotnet_executable(cfg))
    executable = shutil.which(executable) or executable
    cwd = repo if repo.is_dir() else F.WORKSPACE_ROOT
    if not F.DRY_RUN and Path(executable).is_file():
        result = subprocess.run([executable, '--version'], cwd=cwd, capture_output=True, text=True)
        actual = result.stdout.strip()
        if result.returncode == 0 and (actual == version if version else actual.startswith(channel + '.')):
            F.log(f'.NET SDK 已可用: {actual}')
            return
    if not F.DRY_RUN and F.cget(cfg, 'dotnet_path'):
        F.err('dotnet_path 指定的 SDK 不满足版本要求；请修正配置，或移除配置以安装工作区 .tools/dotnet')
    destination = local_dotnet().parent
    extension = 'ps1' if F.IS_WIN else 'sh'
    url = f'https://dot.net/v1/dotnet-install.{extension}'
    F.log(f'从 Microsoft 下载 SDK 安装脚本: {url}；SDK {version or channel} -> {destination}')
    if F.DRY_RUN:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dotnet-install-', dir=destination.parent) as temporary:
        installer = Path(temporary) / f'dotnet-install.{extension}'
        with urllib.request.urlopen(url, timeout=60) as response, installer.open('wb') as stream:
            shutil.copyfileobj(response, stream)
        if F.IS_WIN:
            powershell = shutil.which('pwsh') or shutil.which('powershell')
            if not powershell:
                F.err('安装 .NET SDK 需要 PowerShell')
            command = [powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', installer,
                       '-InstallDir', destination, '-NoPath', '-Version' if version else '-Channel', version or channel]
        else:
            command = ['bash', installer, '--install-dir', destination, '--no-path',
                       '--version' if version else '--channel', version or channel]
        F.run(command, cwd)
    F.run([local_dotnet(), '--version'], cwd)


def setup_android(cfg, packages=True):
    if F.HOST_OS != 'linux':
        F.err('本项目 Android 工具链准备需在 Linux 宿主执行')
    sdk, jdk = android_paths(cfg)
    # Reuse Chromium's pinned CIPD packages instead of downloading an unrelated JDK/SDK.
    depot = F.tool_environment(cfg)
    required = [jdk / 'bin/java', jdk / 'bin/javac']
    if packages:
        required += [sdk / 'cmdline-tools/latest/bin/sdkmanager']
    missing = [path for path in required[:2] if not path.is_file()]
    if packages and not list((sdk / 'cmdline-tools').glob('*/bin/sdkmanager')) and not (sdk / 'cmdline-tools/bin/sdkmanager').is_file():
        missing.append(required[-1])
    if F.DRY_RUN or missing:
        if not F.DRY_RUN and not (F.CHROMIUM_SRC / 'DEPS').is_file():
            F.err('请先 fetch update --os android，以准备 Chromium DEPS 固定的 SDK/JDK')
        if not F.DRY_RUN and (F.cget(cfg, 'java_home') and any(not p.is_file() for p in required[:2])):
            F.err(f'配置的 JAVA_HOME 不完整: {jdk}；请修正，或移除配置以使用 Chromium JDK')
        if not (depot / 'gclient').is_file():
            F.setup_depot_tools(cfg)
        if not F.DRY_RUN:
            url = F.cget(cfg, 'chromium_src', default=F.URL_CHROMIUM)
            F.write_gclient(url, True, 'android')
        # Sync against the actual checkout, preserving its chosen Chromium version.
        head = 'HEAD' if F.DRY_RUN else F.run(['git', 'rev-parse', 'HEAD'], F.CHROMIUM_SRC, capture=True)
        F.run([depot / 'gclient', 'sync', '--revision', f'src@{head}', '--nohooks'], F.WORKSPACE_ROOT)
        F.run([depot / 'gclient', 'runhooks'], F.WORKSPACE_ROOT)
    if not F.DRY_RUN and any(not p.is_file() for p in required[:2]):
        F.err(f'JDK 仍不完整: {jdk}；请检查 DEPS 同步日志或 JAVA_HOME 配置')
    if not F.DRY_RUN:
        os.environ['JAVA_HOME'] = str(jdk.resolve())
        os.environ['PATH'] = str(jdk / 'bin') + os.pathsep + os.environ.get('PATH', '')
    F.run([jdk / 'bin/java', '-version'])
    F.run([jdk / 'bin/javac', '-version'])
    if not packages:
        return
    managers = sorted((sdk / 'cmdline-tools').glob('*/bin/sdkmanager'))
    if (sdk / 'cmdline-tools/bin/sdkmanager').is_file():
        managers.append(sdk / 'cmdline-tools/bin/sdkmanager')
    manager = next((p for p in managers if p.parent.parent.name == 'latest'), managers[-1] if managers else required[-1])
    if not F.DRY_RUN and not manager.is_file():
        F.err(f'缺少 sdkmanager: {sdk}；检查 android_home 是否指向完整 SDK')
    # Packages are explicit because browser and Chromium compileSdk need not match.
    requested = F.cget(cfg, 'android_sdk_packages').split()
    F.run([manager, f'--sdk_root={sdk}', '--list_installed'])
    if requested:
        # Keep stdin interactive: never pipe yes or silently accept licenses.
        F.run([manager, f'--sdk_root={sdk}', *requested])
    F.log('Android SDK/JDK 已准备；项目要求的 JDK 主版本和额外 SDK 包仍以 Gradle 配置为准')


def setup(name, cfg):
    if name == 'metal':
        setup_metal(cfg)
    elif name == 'dotnet':
        setup_dotnet(cfg)
    else:
        setup_android(cfg, packages=name == 'android-sdk')
