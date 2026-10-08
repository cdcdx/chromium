"""Browser adapter tests with local project/SDK fixtures; no external builds."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build
import fetch
import nomad

VERSION = '1.2.3.4'


class NomadTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='nomad test ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, name, value in ((build, 'ROOT', self.root), (fetch, 'DRY_RUN', False),
                                    (fetch, 'WORKSPACE_ROOT', self.root)):
            setting = patch.object(module, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.args = build.build_parser().parse_args(['nomad_desktop', '--ver', VERSION, '--no-web'])
        self.args.dist_dir = self.root / 'dist'

    def quiet(self, fn, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args)

    def pc_fixture(self, target_os):
        folder = {'win': 'NomadBrowser.Avalonia', 'linux': 'NomadBrowser.Avalonia.Linux', 'mac': 'NomadBrowser.Avalonia.Mac'}[target_os]
        repo = self.root / 'nomad_desktop'
        for name in (folder, 'NomadBrowser.Updater', 'NomadBrowser.Windows.Updater'):
            project = repo / name / (name + '.csproj')
            project.parent.mkdir(parents=True, exist_ok=True)
            project.touch()
        delivery = self.root / 'kernel-delivery'
        (delivery / 'kernel').mkdir(exist_ok=True, parents=True)
        library = {'win': 'arupa_kernel.dll', 'mac': 'libarupa_kernel.dylib', 'linux': 'libarupa_kernel.so'}[target_os]
        (delivery / 'kernel' / library).touch()
        (delivery / 'kernel/.arupa-version').write_text(VERSION)
        self.args.delivery = delivery

    def android_fixture(self):
        repo = self.root / 'nomad_android'
        repo.mkdir()
        (repo / 'gradlew').touch()
        for arch, abi in (('arm64', 'arm64-v8a'), ('x64', 'x86_64')):
            aar = repo / f'app/libs/kernel/{arch}/arupa-kernel.aar'
            aar.parent.mkdir(parents=True)
            with zipfile.ZipFile(aar, 'w') as archive:
                archive.writestr(f'jni/{abi}/libarupakernel.so', b'fixture')
        return repo

    def test_cli_routes_browser_matrix_without_gn(self):
        for target_os, arches in build.MATRIX.items():
            project = 'nomad_android' if target_os == 'android' else 'nomad_desktop'
            with patch.object(nomad, 'run') as run:
                self.quiet(build.main, [project, '--os', target_os, '--arch', 'all', '--ver', VERSION, '--dry-run'])
                self.assertEqual(run.call_args.args[4], arches)
                self.assertEqual(run.call_args.args[6], {'build', 'package'})
        with patch.object(nomad, 'run') as run:
            self.quiet(build.main, ['nomad_desktop', '--os', 'macos', '--ver', VERSION, '--arch', 'x64', '--dry-run'])
            self.assertEqual(run.call_args.args[3], 'mac')

    def test_pc_publish_uses_requested_rid_and_platform(self):
        fetch.DRY_RUN = True
        for target_os in ('win', 'mac', 'linux'):
            self.pc_fixture(target_os)
            for arch in build.MATRIX[target_os]:
                with patch.object(fetch, 'run') as run:
                    self.quiet(nomad.desktop_build, self.root, self.args, target_os, arch, VERSION, self.root / 'out/result')
                commands = [list(map(str, call.args[0])) for call in run.call_args_list]
                main = next(cmd for cmd in commands if 'publish' in cmd)
                self.assertIn(f'{"osx" if target_os == "mac" else target_os}-{arch}', main)
                self.assertIn(f'-p:Platform={arch}', main)
                expected = Path(nomad.__file__).resolve().parent.parent / 'build/nuget.config'
                for command in commands:
                    if 'publish' in command:
                        self.assertIn(f'-p:RestoreConfigFile={expected}', command)

    def test_custom_nuget_config_reaches_facade_and_all_publish_commands(self):
        """自定义 NuGet 配置必须传到每一条 publish 命令上。

        门面重建那条命令不在这里数：dry-run 只记日志、不发构建命令，它由
        tests/test_scripts.py::test_stale_facade_dll_is_rebuilt_from_the_delivery_sources
        直接断言（含 restore props 透传）。
        """
        self.pc_fixture('win')
        facade = self.args.delivery / 'dotnet/ArupaKernel.csproj'
        facade.parent.mkdir()
        facade.touch()
        self.args.nuget_config = self.root / 'private feed.config'
        self.args.nuget_config.write_text('<configuration/>')
        fetch.DRY_RUN = True
        with patch.object(fetch, 'run') as run:
            self.quiet(nomad.desktop_build, self.root, self.args, 'win', 'x64', VERSION, self.root / 'out')
        commands = [list(map(str, call.args[0])) for call in run.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertTrue(all('publish' in command for command in commands))
        for command in commands:
            self.assertIn(f'-p:RestoreConfigFile={self.args.nuget_config.resolve()}', command)
        self.args.nuget_config.unlink()
        with patch.object(fetch, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'NuGet 配置不存在'):
                nomad.desktop_build(self.root, self.args, 'win', 'x64', VERSION, self.root / 'out')
            run.assert_not_called()

    def test_android_both_abis_build_and_package_separately(self):
        repo = self.android_fixture()
        def fake_gradle(command, cwd):
            arch = next(value.split('=')[1] for value in command if str(value).startswith('-PkernelAbi='))
            abi = {'arm64': 'arm64-v8a', 'x64': 'x86_64'}[arch]
            apk = repo / 'app/build/outputs/apk/release/app.apk'
            apk.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr(f'lib/{abi}/libarupakernel.so', b'fixture')
        with patch.object(fetch, 'run', side_effect=fake_gradle), patch.object(nomad, 'prepare_android_sdk'):
            self.quiet(nomad.run, self.root, self.args, 'nomad_android', 'android',
                       ('arm64', 'x64'), VERSION, {'build', 'package'})
        deliveries = sorted(self.args.dist_dir.iterdir())
        # 交付目录名：nomad-<os>-<arch>-<ver>-<release|debug>-<n>
        self.assertEqual([path.name for path in deliveries],
                         [f'nomad-android-arm64-{VERSION}-release-1',
                          f'nomad-android-x64-{VERSION}-release-1'])
        # 构建目录与交付目录同名（不含序号）
        for arch in ('arm64', 'x64'):
            self.assertTrue((self.root / f'out/nomad-android-{arch}-{VERSION}-release').is_dir())
        for path in deliveries:
            manifest = json.loads((path / 'build-manifest.json').read_text())
            nomad.validate_apk(path / 'app.apk', manifest['identity']['arch'])
            self.assertTrue((path / 'SHA256SUMS.txt').is_file())

    def test_mac_invalid_delivery_stops_before_web_and_publish(self):
        self.pc_fixture('mac')
        self.args.no_web = False
        with patch.object(nomad, 'prepare_dotnet', return_value='dotnet'), \
             patch.object(nomad, 'prepare_web_tools') as web, \
             patch.object(fetch, 'run', side_effect=RuntimeError('missing hyphen-data')) as run:
            with self.assertRaisesRegex(RuntimeError, 'missing hyphen-data'):
                nomad.desktop_build(self.root, self.args, 'mac', 'x64', VERSION, self.root / 'output')
        web.assert_not_called()
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn('-t:ValidateArupaDelivery', command)
        self.assertIn('-p:RuntimeIdentifier=osx-x64', command)

    def test_desktop_ninja_builds_hyphen_data(self):
        for target_os in ('win', 'mac', 'linux'):
            self.assertIn('//third_party/hyphenation-patterns:bundle_hyphen_data',
                          build.build_targets('arupa_desktop', target_os))

    def test_wrong_apk_and_changed_output_cannot_be_packaged(self):
        apk = self.root / 'bad.apk'
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('lib/x86_64/native.so', b'fixture')
        with self.assertRaises(RuntimeError):
            nomad.validate_apk(apk, 'arm64')
        identity = {'project': 'test'}
        output = self.root / 'output'
        output.mkdir()
        (output / 'file').write_text('original')
        nomad.seal(output, identity)
        (output / 'file').write_text('modified')
        with self.assertRaises(RuntimeError):
            nomad.package(self.args, output, identity)
        self.assertFalse(self.args.dist_dir.exists())

    def test_dotnet_requires_sdk_and_checks_project_global_json(self):
        self.args.dotnet = '/custom/dotnet'
        repo = self.root / 'nomad_desktop'
        with patch.dict(os.environ, os.environ.copy()), patch.object(nomad.shutil, 'which', return_value='/custom/dotnet'), patch.object(nomad.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '10.0.100\n', '')
            self.assertEqual(self.quiet(nomad.prepare_dotnet, self.args, repo), '/custom/dotnet')
            self.assertEqual(run.call_args.kwargs['cwd'], repo)
            self.assertEqual(os.environ['DOTNET_ROOT'], '/custom')
            self.assertTrue(os.environ['PATH'].startswith('/custom' + os.pathsep))
            run.return_value = subprocess.CompletedProcess([], 1, '', 'SDK missing')
            with self.assertRaisesRegex(RuntimeError, 'Runtime'):
                nomad.prepare_dotnet(self.args, repo)

    def test_android_sdk_validation_environment_and_conflicts(self):
        repo = self.root / 'android'
        repo.mkdir()
        sdk = self.root / 'Android SDK'
        self.args.android_sdk = sdk
        with patch.dict(os.environ, {}, clear=True), patch.object(fetch, 'load_config', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'SDK 不完整'):
                nomad.prepare_android_sdk(self.args, repo)
            for name in ('platforms/android-36/android.jar', 'build-tools/36.0.0/aapt2'):
                path = sdk / name
                path.parent.mkdir(parents=True)
                path.touch()
            with patch.object(nomad.shutil, 'which', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'JDK'):
                    nomad.prepare_android_sdk(self.args, repo)
            with patch.object(nomad.shutil, 'which', return_value='/java'), patch.object(nomad.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, '', 'java version')
                self.quiet(nomad.prepare_android_sdk, self.args, repo)
                self.assertEqual(os.environ['ANDROID_HOME'], str(sdk.resolve()))
                self.assertEqual(os.environ['ANDROID_SDK_ROOT'], str(sdk.resolve()))
            local = repo / 'local.properties'
            local.write_text('sdk.dir=/another/sdk\n')
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                nomad.prepare_android_sdk(self.args, repo)
            self.assertEqual(local.read_text(), 'sdk.dir=/another/sdk\n')

    def test_dotnet_rejects_old_sdk_before_web_build(self):
        self.pc_fixture('mac')
        repo = self.root / 'nomad_desktop'
        project = repo / 'NomadBrowser.Avalonia.Mac/NomadBrowser.Avalonia.Mac.csproj'
        project.write_text('<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>')
        self.args.no_web = False
        with patch.object(nomad.shutil, 'which', return_value='/custom/dotnet'), \
             patch.object(nomad.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '6.0.301', '')), \
             patch.object(fetch, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'net10.0'):
                nomad.desktop_build(self.root, self.args, 'mac', 'x64', VERSION, self.root / 'out')
            run.assert_not_called()

    def test_web_selects_compatible_nvm_node_and_matching_npm(self):
        web = self.root / 'web'
        web.mkdir()
        (web / 'package.json').write_text('{"engines":{"node":">=22.6.0"}}')
        nvm = self.root / 'nvm'
        paths = []
        for version in ('20.5.1', '22.23.3'):
            folder = nvm / f'versions/node/v{version}/bin'
            folder.mkdir(parents=True)
            (folder / 'node').touch()
            (folder / 'npm').touch()
            paths.append(folder / 'node')
        with patch.dict(os.environ, {'NVM_DIR': str(nvm)}), \
             patch.object(fetch, 'IS_WIN', False), patch.object(fetch, 'load_config', return_value={}), \
             patch.object(nomad.shutil, 'which', return_value=str(paths[0])), \
             patch.object(nomad.subprocess, 'run', side_effect=[
                 subprocess.CompletedProcess([], 1, 'v20.5.1', ''),
                 subprocess.CompletedProcess([], 0, 'v22.23.3', '')]) as check, \
             patch.object(fetch, 'run') as run:
            self.assertEqual(self.quiet(nomad.prepare_web_tools, web), str(paths[1].parent / 'npm'))
            self.assertTrue(os.environ['PATH'].startswith(str(paths[1].parent) + os.pathsep))
            self.assertEqual(check.call_args.args[0][-1], '>=22.6.0')
            run.assert_called_once_with([paths[1].parent / 'npm', '--version'], web)

    def test_web_explicit_invalid_node_is_not_silently_replaced(self):
        web = self.root / 'web'
        web.mkdir()
        (web / 'package.json').write_text('{"engines":{"node":">=22.6.0"}}')
        with patch.object(fetch, 'load_config', return_value={'node_path': str(web / 'missing')}), \
             patch.object(fetch, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Node.js >=22.6.0'):
                nomad.prepare_web_tools(web)
            run.assert_not_called()
        fetch.DRY_RUN = True
        with patch.object(nomad.subprocess, 'run', side_effect=AssertionError('executed')):
            self.quiet(nomad.prepare_web_tools, web)

    def test_webui_install_command_is_resilient_and_uses_configured_mirror(self):
        web = self.root / 'web'
        web.mkdir()
        (web / 'package-lock.json').write_text('{}')
        with patch.object(fetch, 'load_config', return_value={'npm_registry': 'https://registry.npmmirror.com'}):
            command = list(map(str, nomad.npm_install_command('npm', web)))
        self.assertEqual(command[:2], ['npm', 'ci'])
        for flag in ('--no-audit', '--no-fund', '--prefer-offline', '--fetch-retries=5',
                     '--fetch-retry-maxtimeout=120000', '--fetch-timeout=600000',
                     '--registry=https://registry.npmmirror.com'):
            self.assertIn(flag, command)
        (web / 'package-lock.json').unlink()
        with patch.object(fetch, 'load_config', return_value={}):
            command = list(map(str, nomad.npm_install_command('npm', web)))
        self.assertEqual(command[:2], ['npm', 'install'])
        self.assertFalse([item for item in command if item.startswith('--registry=')])

    def test_half_installed_webui_modules_are_reinstalled_with_retry(self):
        self.args.no_web = False
        self.pc_fixture('linux')
        web = self.root / 'nomad_desktop/NomadWebUI/nomadwebui'
        web.mkdir(parents=True)
        (web / 'package.json').write_text('{}')
        (web / 'node_modules').mkdir()  # 中断残留：没有 npm 的成功标记
        fetch.DRY_RUN = True
        with patch.object(nomad, 'prepare_dotnet', return_value='dotnet'), \
             patch.object(nomad, 'prepare_web_tools', return_value='npm'), \
             patch.object(fetch, 'run') as run:
            self.quiet(nomad.desktop_build, self.root, self.args, 'linux', 'x64', VERSION, self.root / 'out')
        installs = [call for call in run.call_args_list
                    if list(map(str, call.args[0]))[1:2] == ['install']]
        self.assertEqual(len(installs), 1)
        self.assertTrue(installs[0].kwargs.get('retry'))
        (web / 'node_modules/.package-lock.json').write_text('{}')  # 装成功的标记
        with patch.object(nomad, 'prepare_dotnet', return_value='dotnet'), \
             patch.object(nomad, 'prepare_web_tools', return_value='npm'), \
             patch.object(fetch, 'run') as run:
            self.quiet(nomad.desktop_build, self.root, self.args, 'linux', 'x64', VERSION, self.root / 'out')
        self.assertFalse([call for call in run.call_args_list
                          if list(map(str, call.args[0]))[1:2] in (['ci'], ['install'])])

    def test_toolchain_dry_run_never_executes(self):
        fetch.DRY_RUN = True
        with patch.object(nomad.subprocess, 'run', side_effect=AssertionError('executed')):
            self.quiet(nomad.prepare_dotnet, self.args, self.root)
            self.quiet(nomad.prepare_android_sdk, self.args, self.root)

    def test_failed_rebuild_invalidates_old_manifest(self):
        output = self.root / f'out/nomad-android-arm64-{VERSION}-release'
        output.mkdir(parents=True)
        (output / 'build-manifest.json').write_text('{}')
        with patch.object(nomad, 'android_build', side_effect=RuntimeError('build failed')):
            with self.assertRaises(RuntimeError):
                nomad.run(self.root, self.args, 'nomad_android', 'android', ('arm64',), VERSION, {'build', 'package'})
        self.assertFalse((output / 'build-manifest.json').exists())
        self.assertFalse(self.args.dist_dir.exists())

    def test_rebuild_replaces_existing_output_by_rename_not_delete(self):
        # 产物目录已存在时只允许"改名搁置"：曾误用不存在的 os.delete，导致每次重建都崩。
        output = self.root / 'out/output'
        output.mkdir(parents=True)
        (output / 'stale.txt').write_text('old')
        payload = self.root / 'payload'
        payload.mkdir()
        (payload / 'NomadBrowser').write_text('new')
        self.quiet(nomad.save_output, payload, output)
        self.assertEqual((output / 'NomadBrowser').read_text(), 'new')
        trashed = list(output.parent.glob('output.trash-*'))
        self.assertEqual(len(trashed), 1)
        self.assertEqual((trashed[0] / 'stale.txt').read_text(), 'old')

    def test_linux_kernel_host_is_published_into_arupa_desktop(self):
        # OOP 宿主必须落在 arupa-desktop/（UI 侧按 <ArupaDir>/<exe> 探，chromium 还要同目录找 .pak）。
        repo = self.root / 'nomad_desktop'
        project = repo.joinpath(*nomad.LINUX_KERNELHOST_PROJ)
        project.parent.mkdir(parents=True)
        project.touch()
        kernel_dir = self.root / 'payload/arupa-desktop'
        kernel_dir.mkdir(parents=True)
        commands = []

        def fake_run(cmd, cwd=None, **kwargs):
            commands.append([str(item) for item in cmd])
            (kernel_dir / nomad.LINUX_KERNELHOST_EXE).write_text('apphost')

        with patch.object(fetch, 'run', side_effect=fake_run):
            self.quiet(nomad.publish_linux_kernelhost, repo, kernel_dir, 'dotnet', 'Release',
                       'linux-x64', ['-p:Platform=x64'])
        publish = commands[0]
        self.assertEqual(publish[:3], ['dotnet', 'publish', str(project)])
        self.assertIn('linux-x64', publish)
        self.assertIn('--self-contained', publish)
        self.assertIn('-p:Platform=x64', publish)
        self.assertEqual(publish[publish.index('-o') + 1], str(kernel_dir))
        exe = kernel_dir / nomad.LINUX_KERNELHOST_EXE
        self.assertTrue(os.access(exe, os.X_OK))

    def test_linux_kernel_host_project_and_apphost_are_required(self):
        with self.assertRaisesRegex(RuntimeError, 'KernelHost'):
            self.quiet(nomad.publish_linux_kernelhost, self.root / 'nomad_desktop',
                       self.root / 'kernel', 'dotnet', 'Release', 'linux-x64', [])
        repo = self.root / 'nomad_desktop'
        project = repo.joinpath(*nomad.LINUX_KERNELHOST_PROJ)
        project.parent.mkdir(parents=True)
        project.touch()
        with patch.object(fetch, 'run'), self.assertRaisesRegex(RuntimeError, '没有发布出来'):
            self.quiet(nomad.publish_linux_kernelhost, repo, self.root / 'kernel', 'dotnet',
                       'Release', 'linux-x64', [])

    def test_package_prunes_superseded_deliveries_and_keeps_two(self):
        identity = {'os': 'linux', 'arch': 'x64', 'version': VERSION, 'variant': 'release'}
        output = self.root / f'out/nomad-linux-x64-{VERSION}-release'
        output.mkdir(parents=True)
        (output / 'NomadBrowser').write_text('fixture')
        nomad.seal(output, identity)
        for number in (1, 2, 3):
            (self.args.dist_dir / f'nomad-linux-x64-{VERSION}-release-{number}').mkdir(parents=True)
        others = [self.args.dist_dir / f'arupa-linux-x64-{VERSION}-static-1',
                  self.args.dist_dir / f'nomad-linux-x64-{VERSION}-debug-1']
        for path in others:
            path.mkdir()
        self.quiet(nomad.package, self.args, output, identity)
        names = sorted(p.name for p in self.args.dist_dir.iterdir())
        self.assertIn(f'nomad-linux-x64-{VERSION}-release-3', names)      # 保留最近一份旧交付
        self.assertIn(f'nomad-linux-x64-{VERSION}-release-4', names)      # 本次打包
        self.assertNotIn(f'nomad-linux-x64-{VERSION}-release-1', names)
        self.assertNotIn(f'nomad-linux-x64-{VERSION}-release-2', names)
        for path in others:
            self.assertTrue(path.is_dir(), path)                          # 内核交付/其它 variant 不受影响

    def test_package_cleans_stale_leftovers_and_keep_zero_keeps_history(self):
        identity = {'os': 'linux', 'arch': 'x64', 'version': VERSION, 'variant': 'release'}
        output = self.root / f'out/nomad-linux-x64-{VERSION}-release'
        output.mkdir(parents=True)
        (output / 'NomadBrowser').write_text('fixture')
        nomad.seal(output, identity)
        junk = [output.parent / f'nomad-linux-x64-{VERSION}-release.trash-abcdef01',
                output.parent / '.publish-abc123',
                self.args.dist_dir / '.browser-package-zzy.trash-0f0f0f0f']
        for path in junk:
            path.mkdir(parents=True)
        history = self.args.dist_dir / f'nomad-linux-x64-{VERSION}-release-1'
        history.mkdir()
        self.args.keep = 0
        self.quiet(nomad.package, self.args, output, identity)
        for path in junk:
            self.assertFalse(path.exists(), path)
        self.assertTrue(history.is_dir())                                 # keep=0：只清残留，保留全部历史
        self.assertTrue((self.args.dist_dir / f'nomad-linux-x64-{VERSION}-release-2').is_dir())
        self.assertTrue(output.is_dir())                                  # 构建目录不是残留，不能被清

    def test_zip_preserves_symlinks_and_duplicate_delivery_is_rejected(self):
        output = self.root / 'browser-output'
        output.mkdir()
        (output / 'binary').write_text('fixture')
        if os.name != 'nt':
            (output / 'link').symlink_to('binary')
        identity = {'os': 'mac', 'arch': 'arm64', 'version': VERSION, 'variant': 'release'}
        nomad.seal(output, identity)
        self.args.zip = True
        self.args.num = 1
        delivery = f'nomad-mac-arm64-{VERSION}-release-1'
        self.quiet(nomad.package, self.args, output, identity)
        with zipfile.ZipFile(self.args.dist_dir / f'{delivery}.zip') as archive:
            if os.name != 'nt':
                info = archive.getinfo(f'{delivery}/link')
                self.assertEqual(info.external_attr >> 16 & 0o170000, 0o120000)
        with self.assertRaises(RuntimeError):
            nomad.package(self.args, output, identity)


if __name__ == '__main__':
    unittest.main()
