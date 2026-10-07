"""Host dependencies and read-only kernel preflight, tied to the checked-out Chromium."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import fetch as F
from platforms import host_for


def require_files(paths, hint):
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        F.err('缺少编译工具/依赖:\n' + '\n'.join(missing) + '\n' + hint)


def linux_command(target_os, arches, check=False):
    command = [sys.executable, F.CHROMIUM_SRC / 'build/install-build-deps.py', '--no-chromeos-fonts']
    if target_os == 'android':
        command += ['--android']
    if 'x86' in arches or target_os == 'android':
        command += ['--lib32']
    if check:
        command += ['--quick-check']
    return command


APT_NOISE = (
    'is already the newest version', 'set to manually installed', 'Reading package lists',
    'Building dependency tree', 'Reading state information', 'NOTE: This is only a simulation',
    'The following additional packages will be installed', 'The following packages will be upgraded',
)


def apt_error_excerpt(message, limit=25):
    """只保留 apt 的报错/依赖冲突行；完整输出里绝大多数是 "already the newest version"。"""
    kept = [line.rstrip() for line in message.splitlines()
            if line.strip() and not any(token in line for token in APT_NOISE)]
    if len(kept) > limit:
        kept = kept[:limit] + [f'…（另有 {len(kept) - limit} 行，可手动运行 apt-get --just-print install 查看完整输出）']
    return '\n'.join(kept) if kept else message.strip()


def apt_advice(message, arches):
    """把 apt 的真实报错翻译成可以直接执行的修复步骤。"""
    advice = []
    if re.search(r'Unmet dependencies|--fix-broken', message, re.I):
        # 半安装（dpkg 状态非 ii，如 iU）或残留冲突会让任何 apt-get install 失败。
        advice.append('系统存在未满足的依赖（常见于半安装的包）：先执行 sudo apt-get -f install 修复，成功后再重跑本命令')
    unknown = sorted(set(re.findall(r'Unable to locate package ([\w.:+-]+)', message)))
    if unknown:
        advice.append('当前 apt 索引里查不到这些包: ' + ' '.join(unknown) + '；先 sudo apt-get update，仍缺失则检查/更换软件源')
    if re.search(r'no installation candidate', message, re.I):
        advice.append('包存在但没有可安装的候选版本：确认已启用对应仓库（如 universe/updates/backports）并 sudo apt-get update')
    if re.search(r'Could not open lock|frontend lock|lock file', message, re.I):
        advice.append('apt/dpkg 被其它进程占用或权限不足：等它结束后重试（例如 unattended-upgrades 正在运行）')
    if not re.search(r'Unmet dependencies|--fix-broken', message, re.I) and re.search(r'i386', message, re.I):
        advice.append('缺少 32 位 i386 支持：sudo dpkg --add-architecture i386 && sudo apt-get update')
        if 'x86' in arches:
            advice.append('若不需要 x86（32 位）目标，可用 --arch x64 / --arch arm64 跳过 --lib32 依赖')
    if not advice:
        advice.append('按上面 apt 输出修复缺包/仓库问题后重跑本命令')
    return advice


def apt_diagnosis(target_os, arches, output):
    """还原 install-build-deps.py 吞掉的 apt 报错。

    上游在 install_packages() 里只打印 e.stdout，而 check_output 把失败的输出放在
    e.output，所以 `apt-get --just-print install ...` 的真实原因从不显示，只剩
    "You will have to install the above packages yourself"。这里用脚本自己打印的包列表
    重跑一次只读的 --just-print，把原因和建议补回来。"""
    match = re.search(r'Packages required:\n((?:  \S+\n)+)', output or '')
    if not match:
        return ['未从 install-build-deps.py 输出中解析到包列表，请手动运行查看原因：',
                '  ' + ' '.join(str(item) for item in linux_command(target_os, arches))]
    packages = match.group(1).split()
    try:
        result = subprocess.run(['apt-get', '--just-print', 'install', *packages],
                                capture_output=True, text=True,
                                env={**os.environ, 'LANGUAGE': 'en', 'LANG': 'C'})
    except OSError as error:
        return [f'无法复核 apt-get: {error}']
    if not result.returncode:
        return ['apt-get --just-print 复核通过：失败可能与 sudo 权限、apt/dpkg 锁或临时网络有关，请稍后重试']
    message = (result.stderr + result.stdout).strip()
    return [f'apt-get --just-print install 复查失败（exit {result.returncode}），真实报错节选：',
            apt_error_excerpt(message), *apt_advice(message, arches)]


def windows_components(arches):
    components = ['Microsoft.VisualStudio.Workload.NativeDesktop', 'Microsoft.VisualStudio.Component.VC.ATLMFC']
    if 'arm64' in arches:
        components += ['Microsoft.VisualStudio.Component.VC.Tools.ARM64', 'Microsoft.VisualStudio.Component.VC.MFC.ARM64']
    return components


def windows_check(arches):
    # Chromium's resolver owns the VS/SDK version policy. Do not copy version constants here.
    os.environ['DEPOT_TOOLS_WIN_TOOLCHAIN'] = '0'
    script = F.CHROMIUM_SRC / 'build/vs_toolchain.py'
    require_files([script], '请先 fetch Chromium 源码')
    result = subprocess.run([sys.executable, str(script), 'get_toolchain_dir'], cwd=F.CHROMIUM_SRC,
                            capture_output=True, text=True)
    if result.returncode:
        F.err('Windows 工具链检查失败；按 src/docs/windows_build_instructions.md 安装 VS、ATL/MFC、SDK 和 Debugging Tools。\n' + result.stderr.strip())
    fields = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'(vs_path|sdk_path|sdk_version)\s*=\s*(".*")', line.strip())
        if match:
            fields[match[1]] = json.loads(match[2])
    if set(fields) != {'vs_path', 'sdk_path', 'sdk_version'}:
        F.err('无法解析 Chromium Windows 工具链检测输出:\n' + result.stdout)
    vs, sdk = Path(fields['vs_path']), Path(fields['sdk_path'])
    selected = vs / 'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt'
    require_files([selected], '请安装 Visual C++ 工具集')
    vc = vs / 'VC/Tools/MSVC' / selected.read_text(encoding='utf-8-sig').strip()
    paths = [vc / 'include/vector', vc / 'atlmfc/include/afxwin.h', sdk / 'Debuggers/x64/dbghelp.dll']
    for arch in arches:
        cpu = 'x86' if arch == 'x86' else arch
        paths += [vc / f'lib/{cpu}/libcmt.lib', vc / f'atlmfc/lib/{cpu}/atls.lib',
                  sdk / f'Lib/{fields["sdk_version"]}/um/{cpu}/kernel32.lib',
                  sdk / f'Lib/{fields["sdk_version"]}/ucrt/{cpu}/ucrt.lib']
    require_files(paths, '请在 Visual Studio Installer 补齐所选架构的 C++、ATL/MFC、Windows SDK 和 Debugging Tools')


def sysroot_paths(arches):
    script = F.CHROMIUM_SRC / 'build/linux/sysroot_scripts/install-sysroot.py'
    require_files([script], '请先 fetch Chromium')
    mappings = {}
    for node in ast.parse(script.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('ARCH_TRANSLATIONS', 'DEFAULT_TARGET_PLATFORMS'):
                    mappings[target.id] = ast.literal_eval(node.value)
    if set(mappings) != {'ARCH_TRANSLATIONS', 'DEFAULT_TARGET_PLATFORMS'}:
        F.err('当前 Chromium sysroot 映射格式无法解析，请检查 install-sysroot.py')
    records = json.loads((script.parent / 'sysroots.json').read_text())
    paths = []
    for cpu in arches:
        arch = mappings['ARCH_TRANSLATIONS'].get(cpu, cpu)
        key = mappings['DEFAULT_TARGET_PLATFORMS'][arch] + '_' + arch
        paths.append(F.CHROMIUM_SRC / 'build/linux' / records[key]['SysrootDir'] / 'usr/include/stdio.h')
    return paths


def kernel_check(target_os, arches):
    if F.DRY_RUN:
        F.log(f'(dry-run) 检查 {target_os} 内核工具链、宿主依赖和目标架构: {", ".join(arches)}')
        return
    host = host_for(target_os)
    llvm = F.CHROMIUM_SRC / 'third_party/llvm-build/Release+Asserts/bin'
    paths = [llvm / ('clang-cl.exe' if host == 'win' else 'clang'),
             llvm / ('lld-link.exe' if host == 'win' else 'ld64.lld' if host == 'mac' else 'ld.lld')]
    paths += [F.CHROMIUM_SRC / {'win': 'buildtools/win/gn.exe', 'mac': 'buildtools/mac/gn', 'linux': 'buildtools/linux64/gn'}[host],
              F.CHROMIUM_SRC / ('third_party/ninja/ninja.exe' if host == 'win' else 'third_party/ninja/ninja')]
    require_files(paths, '请运行 fetch toolchains，同步当前 Chromium DEPS 并完成 hooks')
    if host == 'win':
        windows_check(arches)
    elif host == 'linux':
        command = linux_command(target_os, arches, check=True)
        require_files([command[1]], '请先 fetch Chromium')
        result = subprocess.run(list(map(str, command)), cwd=F.CHROMIUM_SRC, capture_output=True, text=True)
        if result.returncode:
            F.err(f'Linux 宿主依赖不完整；运行 fetch host-deps --os {target_os} --arch all。\n' + result.stdout + result.stderr)
        if target_os == 'linux':
            require_files(sysroot_paths(arches), '请运行 fetch sysroots --os linux --arch all')
        else:
            config = (F.CHROMIUM_SRC / 'build/config/android/config.gni').read_text()
            values = {}
            for key in ('public_android_sdk_platform_version', 'public_android_sdk_build_tools_version'):
                match = re.search(r'\b' + key + r'\s*=\s*"([^"\n]+)"', config)
                if not match:
                    F.err(f'无法从当前 Chromium config.gni 读取 {key}')
                values[key] = match[1]
            sdk = F.CHROMIUM_SRC / 'third_party/android_sdk/public'
            ndk = F.CHROMIUM_SRC / 'third_party/android_toolchain/ndk'
            paths = [sdk / f'platforms/android-{values["public_android_sdk_platform_version"]}/android.jar',
                     sdk / f'build-tools/{values["public_android_sdk_build_tools_version"]}/aapt2',
                     F.CHROMIUM_SRC / 'third_party/jdk/current/bin/java',
                     F.CHROMIUM_SRC / 'third_party/jdk/current/bin/javac']
            require_files(paths, '请运行 fetch toolchains --os android，同步 NDK、SDK 和 JDK')
            # DEPS 固定的 NDK 是裁剪过的 CIPD 包（只有 simpleperf 和 toolchains，没有
            # source.properties），所以用它自带的 sysroot 判断整包是否下载完整。
            if not list((ndk / 'toolchains/llvm/prebuilt').glob('linux-*/sysroot/usr/include/stdio.h')):
                F.err('Android NDK sysroot 缺失（NDK 未下载完整）；请运行 fetch toolchains --os android')
    F.log(f'{target_os} 内核工具链检查通过')


def setup_host(target_os, arches, installer=None):
    required_host = host_for(target_os)
    if F.DRY_RUN:
        # 预览模式不接触宿主，也不要求当前机器就是目标宿主。
        F.log(f'(dry-run) 安装 {target_os} 宿主依赖（{required_host} 宿主，架构 {", ".join(arches)}）')
        return
    if F.HOST_OS != required_host:
        F.err(f'{target_os} 宿主依赖只能在 {required_host} 安装')
    if required_host == 'linux':
        # Upstream chooses distro-specific packages and owns sudo/interactive prompts.
        # stderr 一边转发一边留档：失败时用其中的包列表复核 apt，补回被上游吞掉的真实原因。
        command = [str(item) for item in linux_command(target_os, arches)]
        try:
            F.run(command, F.CHROMIUM_SRC, tee=True)
        except subprocess.CalledProcessError as error:
            F.err(f'Linux 宿主依赖安装失败（install-build-deps.py 退出码 {error.returncode}）。\n'
                  + '\n'.join(apt_diagnosis(target_os, arches, error.stderr or '')))
    elif required_host == 'mac':
        import toolchains
        toolchains.setup_metal(F.load_config())
    else:
        installer = installer or F.cget(F.load_config(), 'vs_installer_path')
        if installer:
            command = [str(Path(installer).expanduser()), '--wait', '--norestart', '--includeRecommended']
            for component in windows_components(arches):
                command += ['--add', component]
            # An official VS bootstrapper opens its UI for destination/license/SDK selection.
            F.run(command)
        else:
            F.log('未指定 VS 安装器，仅检查已安装的 VS/SDK；可用 --vs-installer 指定微软官方 bootstrapper')
        windows_check(arches)


def setup_sysroots(arches):
    if not F.DRY_RUN and F.HOST_OS != 'linux':
        F.err('Linux sysroot 准备需要 Linux 宿主')
    for arch in arches:
        F.run([sys.executable, F.CHROMIUM_SRC / 'build/linux/sysroot_scripts/install-sysroot.py', '--arch', arch], F.CHROMIUM_SRC)
