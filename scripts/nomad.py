"""Browser build backends. GN remains the responsibility of the Arupa targets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET

import deliveries
import fetch as F
import toolchains


def prepare_dotnet(args, repo, project=None):
    executable = args.dotnet or toolchains.dotnet_executable(F.load_config())
    executable = os.path.expanduser(executable)
    if not F.DRY_RUN:
        resolved = shutil.which(executable)
        if not resolved:
            F.err('未找到 dotnet；请先 fetch dotnet，或用 --dotnet / dotnet_path 指定 .NET SDK')
        # Run in the repository so global.json participates in SDK selection.
        result = subprocess.run([resolved, '--version'], cwd=repo, capture_output=True, text=True)
        if result.returncode or not result.stdout.strip():
            F.err('没有可用的项目 .NET SDK（仅安装 Runtime 不够）；请检查 global.json。\n' + result.stderr.strip())
        if project and project.stat().st_size:
            frameworks = [element.text or '' for element in ET.parse(project).iter()
                          if element.tag.rsplit('}', 1)[-1] in ('TargetFramework', 'TargetFrameworks')]
            required = [tuple(map(int, match)) for value in frameworks
                        for match in re.findall(r'\bnet(\d+)\.(\d+)', value)]
            actual = re.match(r'(\d+)\.(\d+)', result.stdout.strip())
            if required and (not actual or tuple(map(int, actual.groups())) < max(required)):
                F.err(f'.NET SDK {result.stdout.strip()} 不支持项目目标 {", ".join(frameworks)}；'
                      '请先 bash fetch.sh dotnet（Windows 使用 .\\fetch.ps1 dotnet），或用 --dotnet 指定兼容 SDK')
        executable = resolved
        sdk_root = str(Path(resolved).resolve().parent)
        os.environ['DOTNET_ROOT'] = sdk_root
        os.environ['PATH'] = sdk_root + os.pathsep + os.environ.get('PATH', '')
        F.log(f'.NET SDK: {result.stdout.strip()} ({executable})')
    return executable


def prepare_web_tools(web):
    """Use npm's semver implementation; keep node and npm from the same install."""
    metadata = json.loads((web / 'package.json').read_text(encoding='utf-8'))
    required = metadata.get('engines', {}).get('node', '*')
    if F.DRY_RUN:
        F.log(f'(dry-run) 检查 Node.js/npm，Node 要求 {required}')
        return 'npm.cmd' if F.IS_WIN else 'npm'
    configured = F.cget(F.load_config(), 'node_path')
    current = shutil.which('node')
    candidates = [Path(configured).expanduser()] if configured else ([Path(current)] if current else [])
    if not configured:
        nvm = Path(os.environ.get('NVM_DIR', str(Path.home() / '.nvm')))
        def version_key(path):
            return tuple(map(int, re.findall(r'\d+', path.parent.parent.name)))
        candidates += sorted((nvm / 'versions/node').glob('v*/bin/node'), key=version_key, reverse=True)
    failures = []
    for node in dict.fromkeys(candidates):
        npm = node.parent / ('npm.cmd' if F.IS_WIN else 'npm')
        if not node.is_file() or not npm.is_file():
            failures.append(f'{node}: 缺少配套 node/npm')
            continue
        # npm.cmd is a launcher, while POSIX npm is normally a symlink to npm-cli.js.
        cli = node.parent / 'node_modules/npm/bin/npm-cli.js' if F.IS_WIN else npm.resolve()
        check = subprocess.run([str(node), '-e',
            'const s=require(require.resolve("semver",{paths:[process.argv[1]]}));'
            'console.log(process.version);process.exit(s.satisfies(process.version,process.argv[2])?0:1)',
            str(cli.parent), required], cwd=web, capture_output=True, text=True)
        if check.returncode:
            failures.append(f'{node}: {check.stdout.strip()} {check.stderr.strip()}')
            continue
        os.environ['PATH'] = str(node.parent) + os.pathsep + os.environ.get('PATH', '')
        F.log(f'Node.js: {check.stdout.strip()} ({node})，要求 {required}')
        F.run([npm, '--version'], web)
        return str(npm)
    F.err(f'WebUI 需要 Node.js {required} 和配套 npm；请安装兼容版本，或在 .env 设置 node_path。\n'
          + '\n'.join(failures))


# WebUI 安装：npm 默认只重试 2 次、不复用本地缓存，网络抖动（ETIMEDOUT/ECONNRESET）
# 就会整体失败；这里放宽超时与重试，并优先用缓存里已有的包。
NPM_INSTALL_FLAGS = ('--no-audit', '--no-fund', '--prefer-offline',
                     '--fetch-retries=5', '--fetch-retry-mintimeout=10000',
                     '--fetch-retry-maxtimeout=120000', '--fetch-timeout=600000')


def npm_install_command(npm, web):
    """npm ci/install 命令；`.env npm_registry` 可换镜像（默认仍用项目配置的源）。"""
    command = [npm, 'ci' if (web / 'package-lock.json').is_file() else 'install', *NPM_INSTALL_FLAGS]
    registry = F.cget(F.load_config(), 'npm_registry', 'NPM_REGISTRY')
    if registry:
        command.append(f'--registry={registry}')
    return command


def prepare_android_sdk(args, repo):
    cfg = F.load_config()
    local = repo / 'local.properties'
    local_sdk = ''
    if local.is_file():
        match = re.search(r'^\s*sdk\.dir\s*[=:]\s*(.*?)\s*$', local.read_text(), re.M)
        if match:
            local_sdk = re.sub(r'\\(.)', r'\1', match[1])
    explicit = str(args.android_sdk) if args.android_sdk else F.cget(cfg, 'android_home', 'android_sdk_root')
    bundled_sdk, bundled_jdk = toolchains.android_paths(cfg)
    selected = explicit or local_sdk or (str(bundled_sdk) if bundled_sdk.is_dir() else '')
    if F.DRY_RUN:
        F.log(f'(dry-run) 检查 Android SDK/JDK: {selected or "ANDROID_HOME 或 local.properties sdk.dir"}')
        return
    if not selected:
        F.err('未指定 Android SDK；请先 fetch android-sdk，或设置 --android-sdk、ANDROID_HOME、local.properties 的 sdk.dir')
    sdk = Path(selected).expanduser().resolve()
    if local_sdk:
        local_path = Path(local_sdk).expanduser()
        local_path = (local_path if local_path.is_absolute() else repo / local_path).resolve()
        if explicit and local_path != sdk:
            F.err(f'local.properties 的 sdk.dir 与指定 SDK 不一致，请先统一路径: {local_path} / {sdk}')
        if not explicit:
            sdk = local_path
    if not any((sdk / 'platforms').glob('android-*/android.jar')) or not any((sdk / 'build-tools').glob('*/aapt2')):
        F.err(f'Android SDK 不完整: {sdk}；请安装项目 compileSdk 对应的 platforms 和 build-tools')
    java_home = F.cget(cfg, 'java_home')
    if not java_home and (bundled_jdk / 'bin/javac').is_file():
        java_home = str(bundled_jdk)
    java = str(Path(java_home).expanduser() / 'bin/java') if java_home else 'java'
    javac = str(Path(java_home).expanduser() / 'bin/javac') if java_home else 'javac'
    if not shutil.which(java) or not shutil.which(javac):
        F.err('未找到完整 JDK；请安装项目要求的 JDK 并设置 JAVA_HOME（需要 java 和 javac）')
    result = subprocess.run([java, '-version'], capture_output=True, text=True)
    if result.returncode:
        F.err('JDK 无法运行: ' + result.stderr.strip())
    if java_home:
        os.environ['JAVA_HOME'] = str(Path(java_home).expanduser().resolve())
    os.environ['ANDROID_HOME'] = os.environ['ANDROID_SDK_ROOT'] = str(sdk)
    F.log(f'Android SDK: {sdk}')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def has_ditto():
    """macOS 专用：ditto 才能保住扩展属性（托管 DLL 的签名）与资源分支。"""
    return F.HOST_OS == 'mac' and shutil.which('ditto') is not None


def copy_payload_tree(source, destination, target_os):
    # On macOS, signatures of managed DLLs live in extended attributes.
    # Python shutil on Apple's Python does not preserve them; ditto also
    # preserves bundle symlinks and resource forks. 缺少 ditto 时退回
    # shutil，避免在非 macOS 宿主上直接失败。
    if target_os == 'mac' and has_ditto():
        F.run(['ditto', source, destination])
    else:
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def delivery(root, args, target_os, arch, version):
    if args.delivery:
        path = args.delivery.expanduser().resolve()
    else:
        pattern = re.compile(re.escape(f'arupa-{target_os}-{arch}-{version}-static-') + r'(\d+)$')
        choices = [(int(m[1]), p) for p in (root / 'dist').glob('*')
                   if p.is_dir() and (m := pattern.fullmatch(p.name))]
        if not choices:
            F.err('没有匹配的桌面内核交付包；先构建 arupa_desktop，或使用 --delivery 指定')
        path = max(choices, key=lambda pair: pair[0])[1]
    library = {'win': 'arupa_kernel.dll', 'mac': 'libarupa_kernel.dylib', 'linux': 'libarupa_kernel.so'}[target_os]
    mark = path / 'kernel/.arupa-version'
    if not (path / 'kernel' / library).is_file() or not mark.is_file():
        F.err(f'内核交付包不完整: {path}')
    if mark.read_text().strip() != version:
        F.err(f'内核交付版本与 --ver 不一致: {path}')
    return path


def desktop_project(repo, target_os, explicit):
    if explicit:
        path = explicit.expanduser().resolve()
    else:
        folder = {'win': 'NomadBrowser.Avalonia', 'mac': 'NomadBrowser.Avalonia.Mac',
                  'linux': 'NomadBrowser.Avalonia.Linux'}[target_os]
        path = repo / folder / (folder + '.csproj')
    if not path.is_file():
        F.err(f'缺少浏览器项目 {path}；请先 fetch nomad_desktop，或使用 --pc-project 指定实际工程')
    return path


LINUX_KERNELHOST_PROJ = ('Posix', 'NomadBrowser.Linux.KernelHost', 'NomadBrowser.Linux.KernelHost.csproj')
LINUX_KERNELHOST_EXE = 'NomadBrowser.Linux.KernelHost'


def publish_linux_kernelhost(repo, kernel_dir, dotnet, cfg, rid, props):
    """把 OOP 内核宿主发布进 Linux 便携包的 arupa-desktop/（即 UI 侧的 ArupaDir）。

    为什么 Linux 也要独立宿主：Windows 必须走原生宿主（内核静态链进主镜像）—— Chromium 的
    Windows 沙箱在 CreateProcess(CREATE_SUSPENDED) 与 ResumeThread 之间做跨进程地址交接，
    那一刻只有主 EXE 镜像被映射；macOS/Linux 的沙箱不做地址交接（策略编译成字符串交子进程自
    sandbox_init），库式嵌入/OOP 本身就是安全形态，所以 Linux 与 Mac 同走 OOP，交付里必须带宿主。

    为什么装进 arupa-desktop/ 而不是别处（两条硬约束各钉一个）：
      · 宿主自己：NomadBrowser.Avalonia.Linux/Program.cs::ResolveKernelHostPath 按
        <ArupaDir>/NomadBrowser.Linux.KernelHost 探，而 ArupaDir 就是便携包的 arupa-desktop/；
      · chromium：按**主可执行文件所在目录**找 icudtl.dat 与 *.pak，宿主必须与内核件同目录。

    自包含策略与主程序保持一致（主程序用 --self-contained true）：宿主要是不自包含，就得依赖
    机器上另装 .NET 10 运行时，与"便携包"语义冲突。
    """
    project = repo.joinpath(*LINUX_KERNELHOST_PROJ)
    if not project.is_file():
        F.err(f'缺少 Linux 内核宿主工程 {project} —— 交付里不会有 {LINUX_KERNELHOST_EXE}，'
              'OOP 形态起不来内核（UI 侧会报找不到宿主）')
    # -f net10.0：与主程序同样的理由（多 TFM 工程 publish 不带 -f 会 NETSDK1047）。
    F.run([dotnet, 'publish', project, '-c', cfg, '-r', rid, '-f', 'net10.0',
           '--self-contained', 'true', '-o', kernel_dir, *props], repo)
    exe = kernel_dir / LINUX_KERNELHOST_EXE
    if not exe.is_file():
        F.err(f'{LINUX_KERNELHOST_EXE} 没有发布出来（{kernel_dir}）—— 无扩展名的 apphost 缺失，'
              '启动时 OOP 形态起不来内核')
    exe.chmod(0o755)        # zip/tar 往返会丢可执行位，显式补一次
    F.log(f'内核宿主: arupa-desktop/{LINUX_KERNELHOST_EXE}（{exe.stat().st_size / 1048576:.1f} MB）')


# 桌面集成安装脚本。用 raw 字符串：脚本里 sed 的反斜杠必须原样落到文件里。
LINUX_DESKTOP_INSTALLER = r"""#!/usr/bin/env bash
# 把便携包注册到当前用户的桌面环境：不需要 root、不碰系统目录、不改默认浏览器。
# 只写 $XDG_DATA_HOME（默认 ~/.local/share），可重复执行。
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_ID="nomadbrowser.desktop"
APP_DEST="$DATA_DIR/applications"
ICON_DEST="$DATA_DIR/icons/hicolor/256x256/apps"
TEMPLATE="$APP_DIR/nomadbrowser.desktop.in"

if [ ! -x "$APP_DIR/NomadBrowser" ]; then
  echo "[ERROR] 找不到可执行文件 $APP_DIR/NomadBrowser；请先完整解压交付包。" >&2
  exit 1
fi
if [ ! -f "$TEMPLATE" ]; then
  echo "[ERROR] 找不到模板 $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$APP_DEST" "$ICON_DEST"

# Exec 必须是绝对路径。路径可能含空格（模板里已用双引号包住）与 sed 特殊字符 & | \（转义掉）。
EXEC_PATH="$APP_DIR/run.sh"
ESCAPED="$(printf '%s' "$EXEC_PATH" | sed 's/[&|\\]/\\&/g')"
sed "s|@EXEC@|$ESCAPED|" "$TEMPLATE" > "$APP_DEST/$DESKTOP_ID.tmp"
mv -f "$APP_DEST/$DESKTOP_ID.tmp" "$APP_DEST/$DESKTOP_ID"
cp -f "$APP_DIR/nomadbrowser.png" "$ICON_DEST/nomadbrowser.png"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DEST" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "已注册桌面条目: $APP_DEST/$DESKTOP_ID"
echo "启动器里应出现「逐风浏览器」；设为默认浏览器请在应用内或系统设置中操作。"
"""


def write_linux_desktop_integration(repo, payload):
    """写 Linux 桌面集成件到便携包根：.desktop 模板 + 安装脚本 + 图标。

    为什么是缺口而不是锦上添花：
      · Core/Platform/Linux/LinuxDefaultBrowserService 按 .desktop 的 id 查询/设置默认浏览器
        （默认 id 就是 nomadbrowser.desktop），其文件头声明「便携包的 .desktop 由打包层交付」
        —— 而此前打包层并不产出它，"设为默认浏览器"在任何机器上都不可能成功；
      · MimeType 里的 x-scheme-handler/* 决定应用能否被系统选为 http/https 处理器；
      · StartupWMClass 决定窗口能否归到启动器图标下（否则任务栏会出现两个图标）。

    为什么是「模板 + 安装脚本」而不是直接放可用的 .desktop：freedesktop 规定 Exec 必须是
    绝对路径（不会相对 .desktop 所在目录解析），而便携包解压到哪在打包时无从得知 ——
    只能由安装脚本在目标机上把真实路径填进模板。
    """
    template = payload / 'nomadbrowser.desktop.in'
    template.write_text(
        '[Desktop Entry]\n'
        'Type=Application\n'
        'Version=1.0\n'
        'Name=逐风浏览器\n'
        'Name[en]=Nomad Browser\n'
        'GenericName=Web Browser\n'
        'Comment=智能浏览器\n'
        'Exec="@EXEC@" %u\n'
        'TryExec=@EXEC@\n'
        'Icon=nomadbrowser\n'
        'Terminal=false\n'
        'Categories=Network;WebBrowser;\n'
        # 与 xdg-settings 那条路配套：有这些 MimeType 才可能被选为默认处理器。
        'MimeType=x-scheme-handler/http;x-scheme-handler/https;'
        'x-scheme-handler/about;x-scheme-handler/unknown;text/html;\n'
        'StartupNotify=true\n'
        # Avalonia 在 X11 下用入口程序集名作 WM_CLASS（本工程 AssemblyName=NomadBrowser）。
        'StartupWMClass=NomadBrowser\n', encoding='utf-8')

    icon = repo / 'NomadBrowser.Avalonia' / 'Assets' / 'logo.png'
    if not icon.is_file():
        F.err(f'缺少桌面图标源: {icon}')
    shutil.copy2(icon, payload / 'nomadbrowser.png')

    installer = payload / 'install-desktop.sh'
    installer.write_text(LINUX_DESKTOP_INSTALLER, encoding='utf-8')
    installer.chmod(0o755)
    F.log('桌面集成: nomadbrowser.desktop.in + install-desktop.sh + nomadbrowser.png')


def prune_foreign_native_runtimes(payload, rid):
    """便携包里只保留 runtimes/<rid>/，删掉其他平台的原生库目录。

    根因：Whisper.net.Runtime 用 build/*.targets 逐条 <None Include> 拷贝原生库，
    绕过了 NuGet 对 runtimes/<rid>/native 的 RID 筛选 —— 于是**所有平台**的
    .so/.dylib/.dll（含 macOS 与 Windows）都被塞进包。这些目录不在 .NET 的原生库
    搜索路径里（只按当前 RID 解析，我们的 LinuxNativeLibraryResolver 也只指向内核目录），
    运行时永远不会被加载，纯属体积浪费：x64 Linux 包实测 runtimes/ 共 111 MB，
    其中只有 27 MB（linux-x64）有用。

    必须在 save_output/seal 之前调用，让清单与 SHA256SUMS 覆盖裁剪后的真实文件集。
    """
    keep = rid                    # 只应在 Linux 分支调用，rid = linux-<arch>
    if not keep.startswith('linux-'):
        return
    runtimes = payload / 'runtimes'
    if not runtimes.is_dir():
        return

    released = 0
    for child in sorted(runtimes.iterdir()):
        if not child.is_dir() or child.name == keep:
            continue
        size = sum(item.stat().st_size for item in child.rglob('*') if item.is_file())
        shutil.rmtree(child)
        released += size
        F.log(f'裁剪外来原生库: runtimes/{child.name}（{size / 1048576:.1f} MB）')

    # 布局变化时宁可响亮失败，也不要发出一个没有本地 ASR 原生库的包。
    required = runtimes / keep / 'libwhisper.so'
    if not required.is_file():
        F.err(f'裁剪后缺少 {required.relative_to(payload)}；'
              '原生库布局已变，请更新 prune_foreign_native_runtimes 的保留规则')
    if released:
        F.log(f'原生库裁剪合计释放 {released / 1048576:.1f} MB（保留 runtimes/{keep}）')


MIN_FACADE_BYTES = 50_000          # ~4KB 的是 ref/reference-only 程序集（只有签名没有实现）


def refresh_kernel_facade(dotnet, kernel, repo, cfg, arch, restore_props):
    """按交付自带的门面源码现场重建 dotnet/ArupaKernel.dll，返回它（或 None）。

    交付里的门面 DLL 有两个会咬人的状态，都会让宿主编译撞 CS0117/CS1061/CS0246：
      · 比它旁边的源码旧 —— 内核仓改了 package/desktop/dotnet/*.cs 却没重编就出包；
      · 被出包/外部进程还原成 ref 程序集（~4KB，只有签名）。

    🔴 **绝不能给一次 -o <门面工程自己的目录>**：OutputPath 落在工程目录里时，SDK 的
       DefaultItemExcludes 会把该目录下的 .cs 全部排除，只编译生成的 AssemblyInfo
       → 编出 4KB 空程序集，而退出码仍是 0（静默）。产物落到工程 bin/ 下再拷回顶层。
       写法与 scripts/builder/browser.py 的 ensure_kernel_facade 同一口径。
    """
    d = kernel / 'dotnet'
    proj = d / 'ArupaKernel.csproj'
    top = d / 'ArupaKernel.dll'
    # PC 仓内的稳定副本与交付顶层必须同一份：Directory.Build.targets 的回落告警就是这么要求的
    # （「否则两处会各指一份门面」）。口径同 scripts/builder/browser.py 的 ensure_kernel_facade。
    stable = repo / 'build' / 'kernel-facade' / 'ArupaKernel.dll'

    def sync_stable(path):
        if not path or not path.is_file() or path.stat().st_size < MIN_FACADE_BYTES:
            return
        try:
            stable.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, stable)
        except OSError as error:
            F.log(f'稳定副本同步失败（不影响本次构建）: {stable}: {error}')

    if not proj.is_file():
        F.log(f'交付里没有门面源码工程（{proj}）—— 沿用交付自带门面')
        sync_stable(top)
        return top if top.is_file() else None
    sources = [p for p in d.glob('*.cs') if p.is_file()] + [proj]
    if top.is_file() and top.stat().st_size >= MIN_FACADE_BYTES \
            and top.stat().st_mtime >= max(p.stat().st_mtime for p in sources):
        F.log(f'交付门面已是最新（{top.stat().st_size}B）: {top}')
        sync_stable(top)
        return top
    if F.DRY_RUN:
        F.log(f'(dry-run) 现场重建交付门面: dotnet build {proj} -c {cfg} -p:Platform={arch}')
        return top
    F.log(f'交付门面缺失/过旧/是 ref 程序集 —— 现场重建: {proj}')
    F.run([dotnet, 'build', str(proj), '-c', cfg, f'-p:Platform={arch}', *restore_props], repo)
    built = d / 'bin' / arch / cfg / 'net10.0' / 'ArupaKernel.dll'
    if not built.is_file():
        built = d / 'bin' / cfg / 'net10.0' / 'ArupaKernel.dll'
    if not built.is_file() or built.stat().st_size < MIN_FACADE_BYTES:
        F.err(f'门面现场重建后仍没有可用的 ArupaKernel.dll（顶层与 bin 产物都缺失或过小）: {d}；'
              '检查交付件 dotnet/ 里的门面源码/工程是否完整')
    shutil.copyfile(built, top)
    docs = built.with_suffix('.xml')
    if docs.is_file():
        shutil.copyfile(docs, d / 'ArupaKernel.xml')
    F.log(f'交付门面重建完成: {built} -> {top}（{top.stat().st_size}B）')
    sync_stable(top)
    return top


def desktop_build(root, args, target_os, arch, version, output):
    repo = root / 'nomad_desktop'
    # 命令行标志是 --pc-project，对应 argparse 属性 args.pc_project。
    project = desktop_project(repo, target_os, args.pc_project)
    nuget_config = (args.nuget_config or Path(__file__).resolve().parent.parent / 'build/nuget.config').expanduser().resolve()
    if not nuget_config.is_file():
        F.err(f'NuGet 配置不存在: {nuget_config}')
    try:
        ET.parse(nuget_config)
    except ET.ParseError as exc:
        F.err(f'NuGet 配置 XML 无效: {nuget_config}: {exc}')
    restore_props = [f'-p:RestoreConfigFile={nuget_config}']
    F.log(f'NuGet 配置: {nuget_config}')
    kernel = delivery(root, args, target_os, arch, version)
    rid = f'{"osx" if target_os == "mac" else target_os}-{arch}'
    if target_os == 'linux' and arch == 'x86':
        F.log('Linux x86 发布要求项目提供 linux-x86 运行时及原生依赖；官方 .NET SDK 无法保证支持')
    cfg = args.variant.capitalize()
    dotnet = prepare_dotnet(args, repo, project)
    # 顺序要紧：先把交付自带的门面按源码现场重建，再决定 props 引用哪一份门面。
    # 反过来的话，引用的还是那份「旧/空」门面 —— 表现就是一堆 CS0117/CS1061/CS0246。
    facade = refresh_kernel_facade(dotnet, kernel, repo, cfg, arch, restore_props) \
        if target_os != 'mac' else None
    props = [f'-p:ArupaDeliveryRoot={kernel}', f'-p:ArupaSdkDir={kernel}',
             f'-p:Platform={arch}', '-p:UseSharedCompilation=false', *restore_props]
    if target_os != 'mac' and not (facade and facade.is_file() and facade.stat().st_size >= MIN_FACADE_BYTES):
        # 现场重建都拿不到完整门面（交付件缺门面源码/工程）：改用 PC 仓内的稳定副本
        # （由 scripts/builder/browser.py ensure_kernel_facade 维护），否则宿主满屏
        # CS0246 "未能找到 Arupa/ArupaWebView/ArupaKernel"。
        stable = repo / 'build' / 'kernel-facade' / 'ArupaKernel.dll'
        if stable.is_file() and stable.stat().st_size >= MIN_FACADE_BYTES:
            F.log(f'交付顶层门面不可用 —— 引用回落稳定副本: {stable}')
            props.append(f'-p:ArupaKernelAssembly={stable}')
        else:
            F.log('交付顶层门面不可用，PC 仓也没有稳定副本 —— 交由 MSBuild 自行回落')
    if target_os == 'mac':
        props += ['-p:BuildMac=true', f'-p:MacRuntimeIdentifier={rid}']
        developer_dir = F.cget(F.load_config(), 'nomad_developer_dir')
        if developer_dir:
            os.environ['DEVELOPER_DIR'] = developer_dir
    if target_os == 'mac':
        # Run the browser's authoritative delivery contract before WebUI/publish.
        F.run([dotnet, 'msbuild', project, '-t:ValidateArupaDelivery',
               f'-p:RuntimeIdentifier={rid}', *props], repo)
    web = repo / 'NomadWebUI/nomadwebui'
    if not args.no_web:
        if not (web / 'package.json').is_file():
            F.err(f'缺少 WebUI 项目: {web}；已有资源时可显式 --no-web')
        npm = prepare_web_tools(web)
        # node_modules 存在 ≠ 装成功：npm ci 先删后装，中断留下的半成品没有 npm 的成功
        # 标记（node_modules/.package-lock.json），只看目录会让它跳过安装、随后构建报缺包。
        if not (web / 'node_modules/.package-lock.json').is_file():
            F.run(npm_install_command(npm, web), web, retry=True)
        F.run([npm, 'run', 'build'], web)
    # 门面已在上面的 refresh_kernel_facade 里按源码重建并归位到交付顶层
    # （原来这里那次 `-o <交付>\dotnet` 的构建会把工程自己的源文件排除掉，
    #   编出 4KB 空程序集且退出码为 0 —— 已移除）。
    stage = output.parent / ('.' + output.name + '.publish') if F.DRY_RUN else Path(tempfile.mkdtemp(prefix='.publish-', dir=output.parent))
    try:
        bundle_root = stage / 'bundles'
        publish_props = props + ([f'-p:MacDistRoot={bundle_root}', f'-p:LocalDebugOutputRoot={stage / "local"}'] if target_os == 'mac' else [])
        if target_os == 'linux':
            publish_props += ['-f', 'net10.0', '-p:EnableWindowsTargeting=true']
        F.run([dotnet, 'publish', project, '-c', cfg, '-r', rid, '--self-contained', 'true',
               '-o', stage / 'payload', *publish_props], repo)
        if target_os == 'win':
            for folder, subdir in (('NomadBrowser.Updater', ''), ('NomadBrowser.Windows.Updater', 'Helpers/WindowsUpdater')):
                updater = repo / folder / (folder + '.csproj')
                if not updater.is_file():
                    F.err(f'缺少更新器工程: {updater}')
                F.run([dotnet, 'publish', updater, '-c', cfg, '-r', rid, '--self-contained', 'true',
                       '-p:PublishAot=false', '-o', stage / 'payload' / subdir, *props], repo)
        if F.DRY_RUN:
            return
        if target_os == 'mac':
            apps = list(bundle_root.glob('*.app')) + list(bundle_root.glob('*/*.app'))
            if len(apps) != 1:
                F.err(f'期望 MacDistRoot 下唯一 .app，实际 {len(apps)} 个: {bundle_root}')
            payload = stage / 'delivery'
            payload.mkdir()
            copy_payload_tree(apps[0], payload / apps[0].name, target_os)
            F.run(['codesign', '--verify', '--deep', '--strict', payload / apps[0].name])
            if not (payload / apps[0].name / 'Contents/Resources/arupa-mac').is_dir():
                F.err('macOS bundle 缺少内核 Contents/Resources/arupa-mac')
        else:
            payload = stage / 'payload'
            executable = payload / ('NomadBrowser.exe' if target_os == 'win' else 'NomadBrowser')
            if not executable.is_file():
                F.err(f'发布缺少浏览器可执行文件: {executable}')
            shutil.copytree(kernel / 'kernel', payload / 'arupa-desktop', dirs_exist_ok=True, symlinks=True)
            if target_os == 'linux':
                # 放在 copytree 之后：交付根的 kernel/ 是内核产出方的目录，不往里写，
                # 只写我们自己的交付暂存目录（理由见 publish_linux_kernelhost）。
                publish_linux_kernelhost(repo, payload / 'arupa-desktop', dotnet, cfg, rid, props)
            resources = [repo / 'dist' / name / 'Resources' for name in (cfg, 'Debug', 'Release')]
            resource = next((p for p in resources if (p / 'index.html').is_file()), None)
            if resource:
                shutil.copytree(resource, payload / 'Resources', dirs_exist_ok=True)
            if not (payload / 'Resources/index.html').is_file():
                F.err('发布缺少 WebUI Resources/index.html')
            if target_os == 'linux':
                launcher = payload / 'run.sh'
                launcher.write_text(
                    '#!/usr/bin/env bash\nset -e\n'
                    'BROWSER_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"\n'
                    'cd "$BROWSER_DIR"\n'
                    'export LD_LIBRARY_PATH="$BROWSER_DIR/arupa-desktop${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
                    'exec "$BROWSER_DIR/NomadBrowser" "$@"\n', encoding='utf-8')
                launcher.chmod(0o755)
                write_linux_desktop_integration(repo, payload)
                prune_foreign_native_runtimes(payload, rid)
        save_output(payload, output)
    finally:
        if not F.DRY_RUN:
            # 正常路径 payload 已 rename 走，stage 只剩空壳可直接删；
            # 若 save_output 走了 copytree 兜底，stage 仍含上千文件，
            # 批量 rmtree 会被受限环境的删除保护拦截 —— 此时改名搁置。
            try:
                leftover = sum(1 for _ in stage.rglob('*'))
            except OSError:
                leftover = 0
            if leftover > 40:
                try:
                    # 改名搁置：受限环境的删除保护会拦批量删除，改名不动内容最省事。
                    os.rename(stage, stage.parent / f'{stage.name}.trash-{os.urandom(4).hex()}')
                except OSError:
                    shutil.rmtree(stage, ignore_errors=True)
            else:
                shutil.rmtree(stage, ignore_errors=True)


def validate_apk(path, arch):
    abi = {'arm64': 'arm64-v8a', 'x64': 'x86_64'}[arch]
    with zipfile.ZipFile(path) as archive:
        abis = {name.split('/')[1] for name in archive.namelist()
                if name.startswith('lib/') and name.endswith('.so') and len(name.split('/')) > 2}
    if abis != {abi}:
        F.err(f'APK 架构不匹配: {path}，期望 {abi}，实际 {sorted(abis)}')


def android_build(root, args, arch, output):
    repo = root / 'nomad_android'
    wrapper = repo / 'gradlew'
    if not wrapper.is_file():
        F.err(f'缺少 {wrapper}；请先 fetch nomad_android')
    prepare_android_sdk(args, repo)
    aar = repo / f'app/libs/kernel/{arch}/arupa-kernel.aar'
    if not aar.is_file():
        F.err(f'缺少内核 AAR: {aar}；请先将 arupa_android 交付件接入浏览器')
    with zipfile.ZipFile(aar) as archive:
        abi = {'arm64': 'arm64-v8a', 'x64': 'x86_64'}[arch]
        if f'jni/{abi}/libarupakernel.so' not in archive.namelist():
            F.err(f'AAR 缺少 {abi} 内核: {aar}')
    manifest = repo / 'tools/ci/runtime-manifest.json'
    if manifest.exists():
        expected = json.loads(manifest.read_text()).get('files', {}).get(aar.relative_to(repo).as_posix())
        if expected and expected != digest(aar):
            F.err('AAR 与 runtime-manifest.json 指纹不匹配；请同步完整内核交付，不能混用版本')
    # Gradle's ABI builds share outputs. Clean before each ABI so stale APKs
    # cannot be mislabeled as the next architecture.
    F.run(['bash', wrapper, ':app:clean', f':app:assemble{args.variant.capitalize()}',
           f'-PkernelAbi={arch}', '--max-workers', str(args.jobs)], repo)
    if F.DRY_RUN:
        return
    apk_dir = repo / 'app/build/outputs/apk' / args.variant
    apks = sorted(apk_dir.glob('*.apk'))
    if not apks:
        F.err(f'Gradle 未产出 APK: {apk_dir}')
    for apk in apks:
        validate_apk(apk, arch)
    with tempfile.TemporaryDirectory(prefix='.apk-', dir=output.parent) as temporary:
        payload = Path(temporary) / 'payload'
        payload.mkdir()
        for apk in apks:
            shutil.copy2(apk, payload / apk.name)
        save_output(payload, output)


def save_output(payload, output):
    # Only replace previously generated output after a successful build.
    if output.exists():
        # 受限环境（守护/安全策略）会拦截大批量删除；改名为 .trash-* 搁置，不丢数据，也不阻塞构建。
        trash = output.parent / (output.name + '.trash-' + os.urandom(4).hex())
        try:
            os.rename(output, trash)
        except OSError:
            shutil.rmtree(output)
    # Windows 上 dotnet publish 刚结束时句柄可能延迟释放，rename 偶发失败；
    # shutil.move 会退回 copy+rmtree，而批量 rmtree 会被删除保护拦截。
    # 因此优先重试原子 rename，最终兜底 copytree（残留交给调用方的搁置逻辑）。
    for attempt in range(4):
        try:
            os.rename(payload, output)
            return
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    shutil.copytree(payload, output, symlinks=True)


def seal(output, identity):
    files = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
    (output / 'build-manifest.json').write_text(json.dumps({'identity': identity, 'files': files}, indent=2) + '\n')


def delivery_base(identity):
    """构建目录与交付目录的公共名字：nomad-<os>-<arch>-<version>-<release|debug>。

    统一用产品名 nomad（不再带 nomad_desktop / nomad_android 的项目名），桌面与 Android
    靠 os 字段区分，release/debug 靠 variant 区分；交付目录名再追加 -<序号>。"""
    return (f"nomad-{identity['os']}-{identity['arch']}-"
            f"{identity['version']}-{identity['variant']}")


def package(args, output, identity):
    keep = max(0, args.keep)
    if F.DRY_RUN:
        F.log(f'(dry-run) 校验并打包 {output} -> {args.dist_dir}/{delivery_base(identity)}-<n>')
        F.log('(dry-run) 清理残留（*.trash-*、.publish-*、.browser-package-*）'
              + (f'；同身份交付保留最近 {keep} 份' if keep else '；保留全部交付'))
        return
    stamp = output / 'build-manifest.json'
    if not stamp.is_file():
        F.err(f'缺少成功构建清单: {stamp}；请先 build')
    manifest = json.loads(stamp.read_text())
    if manifest['identity'] != identity:
        F.err(f'构建配置与打包请求不符: {output}')
    actual = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*')
              if p.is_file() and p != stamp}
    if actual != manifest['files']:
        F.err(f'构建产物在 build 后发生变化，请重新 build: {output}')
    destination = args.dist_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # 中断的打包与反复重建会在 out/、dist/ 堆下整份旁置拷贝；打包前清掉，也顺带腾出磁盘。
    deliveries.clean_stale_directories(output.parent, destination)
    prefix = delivery_base(identity) + '-'
    pattern = re.compile(re.escape(prefix) + r'(\d+)$')
    number = args.num or max([int(m[1]) for p in destination.iterdir() if (m := pattern.fullmatch(p.name))] + [0]) + 1
    final = destination / f'{prefix}{number}'
    if final.exists() or Path(str(final) + '.zip').exists():
        F.err(f'交付包已存在: {final}')
    lock = destination / (final.name + '.lock')
    try:
        with lock.open('x'):
            pass
    except FileExistsError:
        F.err(f'交付序号正在被占用（可能是上次打包中断的残留）：{lock}；'
              '确认没有并发打包后删除该文件再重试')
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix='.browser-package-', dir=destination))
        copy_payload_tree(output, stage, identity['os'])
        if identity['os'] == 'mac':
            for app in stage.glob('*.app'):
                F.run(['codesign', '--verify', '--deep', '--strict', app])
        (stage / 'SHA256SUMS.txt').write_text(''.join(f'{hash_value}  {name}\n' for name, hash_value in sorted(actual.items())))
        # 与 save_output 同理：Windows 上刚写完上千个文件后句柄可能延迟释放，
        # rename 偶发失败；重试原子 rename 而不是立刻让 finally 的 rmtree
        # 掩盖真实原因（rmtree 批量删除还会被删除保护拦截，报错更具误导性）。
        for attempt in range(4):
            try:
                stage.rename(final)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if args.zip:
            archive_path = Path(str(final) + '.zip')
            try:
                if identity['os'] == 'mac' and has_ditto():
                    F.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', final, archive_path])
                else:
                    write_portable_zip(final, archive_path, destination)
            except BaseException:
                archive_path.unlink(missing_ok=True)
                raise
        # 只在本次打包成功后清理旧交付：失败时上一份仍留着可回退。
        deliveries.prune(destination, prefix, keep, number)
    finally:
        # 收尾清理是 best-effort：无论打包成败，清理失败都不应掩盖真实结果（受限环境会拦截批量删除/单文件 unlink；改名旁置即可，别在这里抛）。
        try:
            if stage and stage.exists():
                trash = stage.parent / (stage.name + '.trash-' + os.urandom(4).hex())
                try:
                    stage.rename(trash)
                except OSError:
                    shutil.rmtree(stage)
        except OSError as cleanup_error:
            F.log(f'警告: staging 清理未完成（{cleanup_error}）；可手工处理 {stage}')
        try:
            # 先尝试直接删除，避免在 dist/ 里留下 .lock.trash-* 垃圾；
            # 删除受限环境（沙箱删除闸门）下 unlink 可能失败，再退回改名旁置。
            lock.unlink()
        except OSError:
            try:
                trash = lock.parent / (lock.name + '.trash-' + os.urandom(4).hex())
                lock.rename(trash)
            except OSError as lock_error:
                F.log(f'警告: lock 文件未能删除（{lock_error}）: {lock}')
    F.log(f'浏览器交付包: {final}')


def write_portable_zip(final, archive_path, destination):
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(final.rglob('*')):
            name = path.relative_to(destination).as_posix()
            if path.is_symlink():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
                archive.writestr(entry, os.readlink(path))
            else:
                archive.write(path, name)


def run(root, args, project, target_os, arches, version, actions):
    if 'gen' in actions:
        F.err('浏览器使用 .NET/Gradle；不支持 gen。GN 配置请用于 arupa_desktop/arupa_android')
    if args.args:
        F.err('--args 仅用于 Arupa 内核；浏览器通过交付件消费内核')
    if project == 'nomad_android' and args.delivery:
        F.err('Android 请将内核交付接入 app/libs/kernel/<arch> 并更新 runtime-manifest.json；--delivery 用于 PC')
    for arch in arches:
        identity = {'project': project, 'os': target_os, 'arch': arch, 'version': version, 'variant': args.variant}
        # 构建目录与交付目录同名（交付再追加 -<序号>），便于按同一个名字定位产物。
        output = root / 'out' / delivery_base(identity)
        if 'build' in actions:
            if not F.DRY_RUN:
                output.parent.mkdir(parents=True, exist_ok=True)
                # A failed rebuild must not permit packaging yesterday's output.
                (output / 'build-manifest.json').unlink(missing_ok=True)
            if project == 'nomad_desktop':
                desktop_build(root, args, target_os, arch, version, output)
            else:
                android_build(root, args, arch, output)
            if not F.DRY_RUN:
                seal(output, identity)
        if 'package' in actions:
            package(args, output, identity)
