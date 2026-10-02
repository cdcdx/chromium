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
    host = 'linux' if target_os == 'android' else target_os
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
                     F.CHROMIUM_SRC / 'third_party/jdk/current/bin/javac',
                     ndk / 'source.properties']
            require_files(paths, '请运行 fetch toolchains --os android，同步 NDK、SDK 和 JDK')
            if not list((ndk / 'toolchains/llvm/prebuilt').glob('linux-*/sysroot/usr/include/stdio.h')):
                F.err('Android NDK sysroot 缺失；请运行 fetch toolchains --os android')
    F.log(f'{target_os} 内核工具链检查通过')


def setup_host(target_os, arches, installer=None):
    required_host = 'linux' if target_os == 'android' else target_os
    if not F.DRY_RUN and F.HOST_OS != required_host:
        F.err(f'{target_os} 宿主依赖只能在 {required_host} 安装')
    if required_host == 'linux':
        # Upstream chooses distro-specific packages and owns sudo/interactive prompts.
        F.run(linux_command(target_os, arches), F.CHROMIUM_SRC)
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
        elif F.DRY_RUN:
            F.log('(dry-run) 检查已安装的 VS/SDK；安装时用 --vs-installer 指定微软官方 VS bootstrapper')
        if not F.DRY_RUN:
            windows_check(arches)


def setup_sysroots(arches):
    if not F.DRY_RUN and F.HOST_OS != 'linux':
        F.err('Linux sysroot 准备需要 Linux 宿主')
    for arch in arches:
        F.run([sys.executable, F.CHROMIUM_SRC / 'build/linux/sysroot_scripts/install-sysroot.py', '--arch', arch], F.CHROMIUM_SRC)
