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
        self.args = build.build_parser().parse_args(['nomadbrowser.pc', '--ver', VERSION, '--no-web'])
        self.args.dist_dir = self.root / 'dist'

    def quiet(self, fn, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args)

    def pc_fixture(self, target_os):
        folder = {'win': 'NomadBrowser.Avalonia', 'linux': 'NomadBrowser.Avalonia.Linux', 'mac': 'NomadBrowser.Avalonia.Mac'}[target_os]
        repo = self.root / 'nomadbrowser.pc'
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
        repo = self.root / 'nomadbrowser.android'
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
            project = 'nomadbrowser.android' if target_os == 'android' else 'nomadbrowser.pc'
            with patch.object(nomad, 'run') as run:
                self.quiet(build.main, [project, '--os', target_os, '--arch', 'all', '--ver', VERSION, '--dry-run'])
                self.assertEqual(run.call_args.args[4], arches)
                self.assertEqual(run.call_args.args[6], {'build', 'package'})
        with patch.object(nomad, 'run') as run:
            self.quiet(build.main, ['nomadbrowser.pc', '--os', 'macos', '--ver', VERSION, '--arch', 'x64', '--dry-run'])
            self.assertEqual(run.call_args.args[3], 'mac')

    def test_pc_publish_uses_requested_rid_and_platform(self):
        fetch.DRY_RUN = True
        for target_os in ('win', 'mac', 'linux'):
            self.pc_fixture(target_os)
            for arch in build.MATRIX[target_os]:
                with patch.object(fetch, 'run') as run:
                    self.quiet(nomad.pc_build, self.root, self.args, target_os, arch, VERSION, self.root / 'out/result')
                commands = [list(map(str, call.args[0])) for call in run.call_args_list]
                main = next(cmd for cmd in commands if 'publish' in cmd)
                self.assertIn(f'{"osx" if target_os == "mac" else target_os}-{arch}', main)
                self.assertIn(f'-p:Platform={arch}', main)
                expected = Path(nomad.__file__).resolve().parent.parent / 'build/nuget.config'
                for command in commands:
                    if 'publish' in command:
                        self.assertIn(f'-p:RestoreConfigFile={expected}', command)

    def test_custom_nuget_config_reaches_facade_and_all_publish_commands(self):
        self.pc_fixture('win')
        facade = self.args.delivery / 'dotnet/ArupaKernel.csproj'
        facade.parent.mkdir()
        facade.touch()
        self.args.nuget_config = self.root / 'private feed.config'
        self.args.nuget_config.write_text('<configuration/>')
        fetch.DRY_RUN = True
        with patch.object(fetch, 'run') as run:
            self.quiet(nomad.pc_build, self.root, self.args, 'win', 'x64', VERSION, self.root / 'out')
        commands = [list(map(str, call.args[0])) for call in run.call_args_list]
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertIn(f'-p:RestoreConfigFile={self.args.nuget_config.resolve()}', command)
        self.args.nuget_config.unlink()
        with patch.object(fetch, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'NuGet 配置不存在'):
                nomad.pc_build(self.root, self.args, 'win', 'x64', VERSION, self.root / 'out')
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
            self.quiet(nomad.run, self.root, self.args, 'nomadbrowser.android', 'android',
                       ('arm64', 'x64'), VERSION, {'build', 'package'})
        deliveries = sorted(self.args.dist_dir.iterdir())
        self.assertEqual(len(deliveries), 2)
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
                nomad.pc_build(self.root, self.args, 'mac', 'x64', VERSION, self.root / 'output')
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
        repo = self.root / 'nomadbrowser.pc'
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
        repo = self.root / 'nomadbrowser.pc'
        project = repo / 'NomadBrowser.Avalonia.Mac/NomadBrowser.Avalonia.Mac.csproj'
        project.write_text('<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>')
        self.args.no_web = False
        with patch.object(nomad.shutil, 'which', return_value='/custom/dotnet'), \
             patch.object(nomad.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '6.0.301', '')), \
             patch.object(fetch, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'net10.0'):
                nomad.pc_build(self.root, self.args, 'mac', 'x64', VERSION, self.root / 'out')
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

    def test_toolchain_dry_run_never_executes(self):
        fetch.DRY_RUN = True
        with patch.object(nomad.subprocess, 'run', side_effect=AssertionError('executed')):
            self.quiet(nomad.prepare_dotnet, self.args, self.root)
            self.quiet(nomad.prepare_android_sdk, self.args, self.root)

    def test_failed_rebuild_invalidates_old_manifest(self):
        output = self.root / f'out/nomadbrowser.android-android-arm64-{VERSION}-release'
        output.mkdir(parents=True)
        (output / 'build-manifest.json').write_text('{}')
        with patch.object(nomad, 'android_build', side_effect=RuntimeError('build failed')):
            with self.assertRaises(RuntimeError):
                nomad.run(self.root, self.args, 'nomadbrowser.android', 'android', ('arm64',), VERSION, {'build', 'package'})
        self.assertFalse((output / 'build-manifest.json').exists())
        self.assertFalse(self.args.dist_dir.exists())

    def test_zip_preserves_symlinks_and_duplicate_delivery_is_rejected(self):
        output = self.root / 'browser-output'
        output.mkdir()
        (output / 'binary').write_text('fixture')
        if os.name != 'nt':
            (output / 'link').symlink_to('binary')
        identity = {'os': 'mac', 'arch': 'arm64'}
        nomad.seal(output, identity)
        self.args.zip = True
        self.args.num = 1
        self.quiet(nomad.package, self.args, output, identity)
        with zipfile.ZipFile(self.args.dist_dir / 'browser-output-1.zip') as archive:
            if os.name != 'nt':
                info = archive.getinfo('browser-output-1/link')
                self.assertEqual(info.external_attr >> 16 & 0o170000, 0o120000)
        with self.assertRaises(RuntimeError):
            nomad.package(self.args, output, identity)


if __name__ == '__main__':
    unittest.main()
