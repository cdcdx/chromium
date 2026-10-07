"""Native toolchain checks with synthetic SDKs; no host installations."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fetch
import native_tools as N


class NativeToolsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (('WORKSPACE_ROOT', self.root), ('CHROMIUM_SRC', self.root / 'src'), ('DRY_RUN', False)):
            setting = patch.object(fetch, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        environment = patch.dict(os.environ, os.environ.copy())
        environment.start()
        self.addCleanup(environment.stop)

    def quiet(self, fn, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args)

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_host_deps_dry_run_matrix_and_no_implicit_install(self):
        with patch('subprocess.run', side_effect=AssertionError('executed')):
            for target_os in ('win', 'mac', 'linux', 'android'):
                self.quiet(fetch.main, ['host-deps', '--os', target_os, '--dry-run'])
        self.assertEqual(list(self.root.iterdir()), [])
        with patch.object(N, 'setup_host') as install:
            self.quiet(fetch.main, ['toolchains', '--os', 'linux', '--ver', '1.2.3.4', '--dry-run'])
            install.assert_not_called()
        with patch.object(N, 'setup_host') as install:
            self.quiet(fetch.main, ['toolchains', '--os', 'linux', '--ver', '1.2.3.4', '--install-host-deps', '--dry-run'])
            install.assert_called_once()

    def test_linux_commands_include_snapshot_host_libraries(self):
        self.assertIn('--lib32', N.linux_command('linux', ('x86',), True))
        self.assertIn('--android', N.linux_command('android', ('arm64',), True))
        self.assertIn('--lib32', N.linux_command('android', ('arm64',), True))
        self.assertIn('--quick-check', N.linux_command('linux', ('x64',), True))
        self.assertNotIn('--quick-check', N.linux_command('linux', ('x64',), False))
        self.assertNotIn('--no-prompt', N.linux_command('linux', ('x64',), False))

    def test_linux_host_deps_failure_reveals_the_swallowed_apt_error(self):
        # 上游只打印 e.stdout（check_output 放在 e.output），真实报错从不显示。
        upstream = ('install-build-deps.py [INFO]: Packages required:\n'
                    '  autoconf\n  libasound2:i386\n'
                    'install-build-deps.py [ERROR]: You will have to install the above packages yourself.\n')
        apt = subprocess.CompletedProcess([], 100, '',
                                          "E: Unmet dependencies. Try 'apt --fix-broken install' with no packages.\n")
        failure = subprocess.CalledProcessError(100, ['python3', 'install-build-deps.py'], stderr=upstream)
        with patch.object(fetch, 'HOST_OS', 'linux'), patch.object(fetch, 'run', side_effect=failure) as run, \
             patch('subprocess.run', return_value=apt) as recheck:
            with self.assertRaisesRegex(RuntimeError, r'apt-get -f install') as caught:
                self.quiet(N.setup_host, 'linux', ('x86', 'x64', 'arm64'))
        message = str(caught.exception)
        self.assertIn('退出码 100', message)
        self.assertIn('Unmet dependencies', message)
        self.assertTrue(run.call_args.kwargs['tee'])
        self.assertEqual(recheck.call_args.args[0][:4], ['apt-get', '--just-print', 'install', 'autoconf'])

    def test_apt_advice_names_missing_packages_and_arch_escape_hatch(self):
        apt = subprocess.CompletedProcess([], 100, '', 'E: Unable to locate package lib32z1:i386\n')
        with patch('subprocess.run', return_value=apt):
            lines = N.apt_diagnosis('linux', ('x86', 'x64'), 'Packages required:\n  lib32z1:i386\n')
        message = '\n'.join(lines)
        self.assertIn('lib32z1:i386', message)
        self.assertIn('apt-get update', message)
        self.assertIn('--arch x64', message)

    def test_apt_excerpt_keeps_conflicts_and_drops_noise(self):
        message = '\n'.join(['Reading package lists...', 'libfoo is already the newest version (1.0).',
                             'clash-verge set to manually installed.',
                             'The following packages have unmet dependencies:',
                             ' libxt-dev : Depends: libsm-dev but it is not going to be installed',
                             *[f'noise {index} is already the newest version' for index in range(40)]])
        excerpt = N.apt_error_excerpt(message)
        self.assertNotIn('already the newest version', excerpt)
        self.assertIn('unmet dependencies', excerpt)
        self.assertIn('libsm-dev', excerpt)
        self.assertLessEqual(len(excerpt.splitlines()), 26)

    def test_apt_diagnosis_falls_back_to_manual_command(self):
        lines = N.apt_diagnosis('linux', ('x64',), 'no package list here')
        self.assertIn('install-build-deps.py', '\n'.join(lines))
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            passing = N.apt_diagnosis('linux', ('x64',), 'Packages required:\n  autoconf\n')
        self.assertIn('复核通过', '\n'.join(passing))

    def test_sysroots_install_only_requested_architectures(self):
        with patch.object(fetch, 'HOST_OS', 'linux'), patch.object(fetch, 'run') as run:
            N.setup_sysroots(('x86', 'arm64'))
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['x86', 'arm64'])

    def test_windows_bootstrap_includes_arm64_and_mfc(self):
        with patch.object(fetch, 'HOST_OS', 'win'), patch.object(fetch, 'run') as run, patch.object(N, 'windows_check') as check:
            N.setup_host('win', ('x86', 'x64', 'arm64'), '/official/vs_bootstrapper.exe')
        command = run.call_args.args[0]
        for component in N.windows_components(('arm64',)):
            self.assertIn(component, command)
        self.assertNotIn('--quiet', command)
        check.assert_called_once_with(('x86', 'x64', 'arm64'))

    def test_windows_checks_libraries_for_each_architecture(self):
        self.touch(fetch.CHROMIUM_SRC / 'build/vs_toolchain.py')
        vs, sdk = self.root / 'VS', self.root / 'SDK'
        version = '14.50'
        selection = self.touch(vs / 'VC/Auxiliary/Build/Microsoft.VCToolsVersion.default.txt')
        selection.write_text(version)
        vc = vs / 'VC/Tools/MSVC' / version
        for path in (vc / 'include/vector', vc / 'atlmfc/include/afxwin.h', sdk / 'Debuggers/x64/dbghelp.dll'):
            self.touch(path)
        for arch in ('x86', 'x64', 'arm64'):
            for path in (vc / f'lib/{arch}/libcmt.lib', vc / f'atlmfc/lib/{arch}/atls.lib',
                         sdk / f'Lib/10.0/um/{arch}/kernel32.lib', sdk / f'Lib/10.0/ucrt/{arch}/ucrt.lib'):
                self.touch(path)
        output = '\n'.join(f'{key} = {json.dumps(str(value))}' for key, value in
                           {'vs_path': vs, 'sdk_path': sdk, 'sdk_version': '10.0'}.items())
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, output, '')):
            N.windows_check(('x86', 'x64', 'arm64'))
            (vc / 'atlmfc/lib/arm64/atls.lib').unlink()
            with self.assertRaisesRegex(RuntimeError, 'arm64'):
                N.windows_check(('arm64',))

    def test_missing_compiler_fails_before_host_commands(self):
        with patch('subprocess.run', side_effect=AssertionError('called')):
            with self.assertRaisesRegex(RuntimeError, 'fetch toolchains'):
                N.kernel_check('linux', ('x64',))

    def test_android_checks_pinned_sdk_and_ndk_sysroot(self):
        src = fetch.CHROMIUM_SRC
        for name in ('third_party/llvm-build/Release+Asserts/bin/clang',
                     'third_party/llvm-build/Release+Asserts/bin/ld.lld',
                     'buildtools/linux64/gn', 'third_party/ninja/ninja', 'build/install-build-deps.py'):
            self.touch(src / name)
        config = self.touch(src / 'build/config/android/config.gni')
        config.write_text('public_android_sdk_platform_version = "37.0"\npublic_android_sdk_build_tools_version = "37.0.0"\n')
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            with self.assertRaisesRegex(RuntimeError, 'android-37.0'):
                N.kernel_check('android', ('arm64', 'x64'))
            # DEPS 固定的 NDK 是裁剪包，没有 source.properties；sysroot 才是下载完成的标志。
            for name in ('third_party/android_sdk/public/platforms/android-37.0/android.jar',
                         'third_party/android_sdk/public/build-tools/37.0.0/aapt2',
                         'third_party/jdk/current/bin/java', 'third_party/jdk/current/bin/javac'):
                self.touch(src / name)
            with self.assertRaisesRegex(RuntimeError, 'NDK sysroot'):
                N.kernel_check('android', ('arm64',))
            self.touch(src / 'third_party/android_toolchain/ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include/stdio.h')
            self.quiet(N.kernel_check, 'android', ('arm64', 'x64'))

    def test_sysroot_manifest_uses_current_chromium_mapping(self):
        installer = self.touch(fetch.CHROMIUM_SRC / 'build/linux/sysroot_scripts/install-sysroot.py')
        installer.write_text("ARCH_TRANSLATIONS = {'x64': 'amd64'}\nDEFAULT_TARGET_PLATFORMS = {'amd64': 'future'}\n")
        (installer.parent / 'sysroots.json').write_text(json.dumps({'future_amd64': {'SysrootDir': 'future-sysroot'}}))
        self.assertEqual(N.sysroot_paths(('x64',)), [fetch.CHROMIUM_SRC / 'build/linux/future-sysroot/usr/include/stdio.h'])

    def test_invalid_platform_options_fail_before_mutation(self):
        with patch.object(fetch, 'setup_depot_tools', side_effect=AssertionError('mutation')):
            for args in (['sysroots', '--os', 'mac'], ['host-deps', '--os', 'android', '--arch', 'x86'],
                         ['dotnet', '--vs-installer', '/installer']):
                with self.assertRaises(SystemExit):
                    self.quiet(fetch.main, [*args, '--dry-run'])
