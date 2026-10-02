"""Offline regression checks for fetch/build orchestration and delivery failures."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fetch
import build

REPO = Path(__file__).resolve().parents[1]
VERSION = '1.2.3.4'


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='arupa test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / 'src'
        self.src.mkdir()
        (self.src / 'chrome').mkdir()
        (self.src / 'chrome/VERSION').write_text('MAJOR=1\nMINOR=2\nBUILD=3\nPATCH=4\n')
        (self.src / 'BUILD.gn').write_text('group("all") {}\n')
        shutil.copytree(REPO / 'build', self.root / 'build')
        for project in ('arupa_desktop', 'arupa_android'):
            (self.root / project).mkdir()
            (self.root / project / 'BUILD.gn').write_text('')
        for module, name, value in ((fetch, 'WORKSPACE_ROOT', self.root), (fetch, 'CHROMIUM_SRC', self.src),
                                    (build, 'ROOT', self.root), (build, 'SRC', self.src), (fetch, 'DRY_RUN', False)):
            obj = patch.object(module, name, value)
            obj.start()
            self.addCleanup(obj.stop)
        env = patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def quiet(self, func, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return func(*args)

    def test_matrix_dry_run_has_no_writes_or_subprocesses(self):
        (self.src / '.git').mkdir()
        (self.root / '.env').write_text(''.join(f'{prefix}_src=https://example.invalid/{name}.git\n{prefix}_ver=v1\n' for name, (prefix, _) in fetch.PROJECTS.items()))
        for name in ('arupa_desktop', 'arupa_android'):
            (self.root / name / '.git').mkdir()
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        with patch('subprocess.run', side_effect=AssertionError('dry-run executed a process')):
            for target_os, arches in build.MATRIX.items():
                for arch in (*arches, 'all'):
                    project = 'arupa_android' if target_os == 'android' else 'arupa_desktop'
                    self.assertEqual(0, self.quiet(build.main, [project, '--os', target_os, '--arch', arch, '--dry-run']))
            self.quiet(fetch.main, ['--ver', VERSION, '--dry-run'])
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.assertEqual(before, after)

    def test_invalid_build_combinations_fail_before_writes(self):
        for args in (['desktop', '--os', 'mac', '--arch', 'x86'],
                     ['android', '--os', 'win'], ['desktop', 'down'],
                     ['desktop', '--link', 'dynamic'], ['android', '--jobs', '0'],
                     ['desktop', '--ver', '9.9.9.9']):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.quiet(build.main, args + ['--dry-run'])

    def test_fetch_order_includes_all_four_versioned_projects(self):
        calls = []
        (self.root / '.env').write_text(''.join(f'{prefix}_src=local\n{prefix}_ver=v1\n' for prefix, _ in fetch.PROJECTS.values()))
        with patch.object(fetch, 'setup_depot_tools', side_effect=lambda *_: calls.append('tools')), \
             patch.object(fetch, 'fetch_chromium', side_effect=lambda *_: calls.append('src') or 'url'), \
             patch.object(fetch, 'write_gclient'), \
             patch.object(fetch, 'fetch_project', side_effect=lambda name, *_: calls.append(name)), \
             patch.object(fetch, 'sync_deps', side_effect=lambda *_: calls.append('deps')):
            self.quiet(fetch.main, ['deps', 'all', 'chromium', '--ver', VERSION])
        self.assertEqual(calls, ['tools', 'src', 'deps', *fetch.PROJECTS])

    def test_fetch_failure_does_not_sync_or_save(self):
        with patch.object(fetch, 'setup_depot_tools'), \
             patch.object(fetch, 'fetch_chromium', side_effect=RuntimeError('fetch failed')), \
             patch.object(fetch, 'sync_deps') as sync:
            with self.assertRaises(RuntimeError):
                self.quiet(fetch.main, ['update', '--ver', VERSION, '--save'])
            sync.assert_not_called()
        self.assertFalse((self.root / '.env').exists())

    def test_env_override_and_proxy_never_changes_global_git(self):
        os.environ['CHROMIUM_VER'] = '2.3.4.5'
        self.assertEqual(fetch.cget({'chromium_ver': VERSION}, 'chromium_ver'), '2.3.4.5')
        with patch('subprocess.run', side_effect=AssertionError('global Git mutation')):
            fetch.apply_proxy({'https_proxy': 'http://localhost:1234'})
        self.assertEqual(os.environ['GIT_CONFIG_VALUE_0'], 'http://localhost:1234')

    def test_android_config_preserves_customizations(self):
        config = "solutions = [{'name': 'src', 'custom_vars': {'foo': True}}]\ntarget_os = ['linux']\n"
        path = self.root / '.gclient'
        path.write_text(config)
        self.quiet(fetch.write_gclient, 'url', True)
        self.quiet(fetch.write_gclient, 'url', True)
        self.assertIn("'foo': True", path.read_text())
        scope = {}
        exec(path.read_text(), scope)
        self.assertTrue({'linux', 'android', fetch.HOST_OS}.issubset(scope['target_os']))

    def test_wrong_mount_is_never_replaced(self):
        mount = self.src / 'chrome/browser/arupa_desktop'
        mount.mkdir(parents=True)
        marker = mount / 'local.cc'
        marker.write_text('local changes')
        with self.assertRaises(RuntimeError):
            build.prepare_project('arupa_desktop')
        self.assertEqual(marker.read_text(), 'local changes')

    def test_android_all_builds_two_abis_and_packages_once(self):
        with patch.object(fetch, 'run') as run:
            self.quiet(build.main, ['android', '--arch', 'all', '--dry-run'])
        commands = [list(map(str, call.args[0])) for call in run.call_args_list]
        builds = [cmd for cmd in commands if '-C' in cmd]
        packages = [cmd for cmd in commands if any('package-arupa_android.sh' in x for x in cmd)]
        self.assertEqual(len(builds), 2)
        self.assertEqual(len(packages), 1)
        self.assertIn('all', packages[0])
        self.assertTrue(all('chrome/browser/arupa_android/aar:arupa_kernel_aar' in cmd for cmd in builds))

    def test_default_kernel_platform_and_architecture(self):
        for host, machine, expected in (('mac', 'arm64', 'arm64'), ('mac', 'x86_64', 'x64'),
                                        ('linux', 'aarch64', 'arm64'), ('win', 'AMD64', 'x64'),
                                        ('win', 'i386', 'x86')):
            with patch.object(fetch, 'HOST_OS', host), patch.object(build.platform, 'machine', return_value=machine), patch.object(fetch, 'run') as run:
                self.quiet(build.main, ['arupa_desktop', 'build', '--dry-run'])
            builds = [list(map(str, call.args[0])) for call in run.call_args_list if '-C' in call.args[0]]
            self.assertEqual(len(builds), 1)
            self.assertTrue(any(f'arupa-{host}-{expected}-' in item for item in builds[0]))
        for extra, count in (([], 2), (['--arch', 'arm64'], 1)):
            with patch.object(fetch, 'run') as run:
                self.quiet(build.main, ['arupa_android', 'build', '--dry-run', *extra])
            builds = [list(map(str, call.args[0])) for call in run.call_args_list if '-C' in call.args[0]]
            self.assertEqual(len(builds), count)
            self.assertTrue(all(any('arupa-android-' in item for item in cmd) for cmd in builds))

    def test_real_git_fetch_pins_tag_and_refuses_dirty_checkout(self):
        remote = self.root / 'remote'
        def git(*args, cwd=remote):
            return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        remote.mkdir()
        git('init')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.invalid')
        (remote / 'version.txt').write_text('first')
        git('add', '.')
        git('commit', '-m', 'first')
        git('tag', VERSION)
        sha = git('rev-parse', 'HEAD')
        (remote / 'version.txt').write_text('second')
        git('commit', '-am', 'second')
        checkout = self.root / 'checkout'
        with patch.object(fetch, 'CHROMIUM_SRC', checkout):
            self.quiet(fetch.fetch_chromium, {'chromium_src': str(remote)}, VERSION, True)
            self.assertEqual(git('rev-parse', 'HEAD', cwd=checkout), sha)
            self.assertEqual(git('rev-parse', f'refs/tags/{VERSION}', cwd=checkout), sha)
            (checkout / 'version.txt').write_text('local')
            with self.assertRaises(RuntimeError):
                self.quiet(fetch.fetch_chromium, {'chromium_src': str(remote)}, VERSION, True)
            self.assertEqual((checkout / 'version.txt').read_text(), 'local')

    def package_fixture(self):
        scripts = self.root / 'scripts/packaging'
        scripts.mkdir(parents=True)
        for name in ('desktop', 'android'):
            shutil.copy(REPO / f'scripts/packaging/package-arupa_{name}.sh', scripts)
        runtime = self.root / 'runtime.js'
        runtime.write_text('// fixture')
        out = self.root / 'output'
        out.mkdir()
        for name in ('content_shell.pak', 'icudtl.dat', 'snapshot_blob.bin', 'arupa_render'):
            (out / name).write_bytes(b'fixture')
        return out, runtime

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS package integration')
    def test_desktop_package_and_arch_mismatch_cleanup(self):
        out, runtime = self.package_fixture()
        # Minimal valid Mach-O header: file(1) can identify the architecture.
        (out / 'libarupa_kernel.dylib').write_bytes(struct.pack('<IiiIIIII', 0xfeedfacf, 0x100000c, 0, 6, 0, 0, 0, 0))
        command = ['bash', str(self.root / 'scripts/packaging/package-arupa_desktop.sh'),
                   '--os', 'mac', '--arch', 'arm64', '--ver', VERSION,
                   '--out', str(out), '--plugin-runtime', str(runtime), '--no-package', '--zip']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        delivery = self.root / f'dist/arupa-mac-arm64-{VERSION}-static-1'
        self.assertTrue((delivery / 'SHA256SUMS.txt').is_file())
        self.assertTrue((delivery / 'MANIFEST.md').is_file())
        self.assertTrue(Path(str(delivery) + '.zip').is_file())
        command[command.index('arm64')] = 'x64'
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / f'dist/arupa-mac-x64-{VERSION}-static-1').exists())
        self.assertTrue(delivery.exists())

    def test_android_wrong_abi_removes_partial_delivery(self):
        out, runtime = self.package_fixture()
        (out / 'apks').mkdir()
        with zipfile.ZipFile(out / 'apks/arupa-kernel.aar', 'w') as aar:
            aar.writestr('jni/x86_64/libarupakernel.so', b'fixture')
        apk = out / 'gen/chrome/browser/arupa_android/aar/arupa_kernel_resources.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'fixture')
        command = ['bash', str(self.root / 'scripts/packaging/package-arupa_android.sh'),
                   '--arch', 'arm64', '--ver', VERSION, '--out', str(out),
                   '--plugin-runtime', str(runtime), '--no-package']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('jni', result.stdout + result.stderr)
        self.assertFalse((self.root / f'dist/arupa-android-{VERSION}-static-1').exists())

    def test_missing_project_config_fails_before_any_fetch(self):
        with patch.object(fetch, 'run', side_effect=AssertionError('network before validation')):
            with self.assertRaises(SystemExit):
                self.quiet(fetch.main, ['all', '--ver', VERSION])

    def test_project_ref_and_path_are_not_shared(self):
        calls = []
        with patch.object(fetch, 'fetch_project', side_effect=lambda *args: calls.append(args)):
            self.quiet(fetch.main, ['arupa_desktop', 'nomadbrowser.android',
                                   '--arupa-desktop-src', 'desktop-url', '--arupa-desktop-ver', 'v2',
                                   '--nomad-android-src', 'android-url', '--nomad-android-ver', 'refs/heads/release'])
        self.assertEqual(calls, [('arupa_desktop', 'desktop-url', 'v2'),
                                 ('nomadbrowser.android', 'android-url', 'refs/heads/release')])

    def test_shared_gn_templates_cover_architecture_and_link_matrix(self):
        for target_os, arches in build.MATRIX.items():
            path = build.args_template(target_os)
            original = path.read_bytes()
            for cpu in arches:
                for mode in (('static',) if target_os == 'android' else ('static', 'dynamic')):
                    text = build.render_args(path, target_os, cpu, mode)
                    self.assertEqual(text.count('target_cpu ='), 1)
                    self.assertIn(f'target_cpu = "{cpu}"', text)
                    self.assertEqual(text.count('is_component_build ='), 1)
                    self.assertIn('is_component_build = ' + ('true' if mode == 'dynamic' else 'false'), text)
                    self.assertIn('proprietary_codecs = true', text)
                    self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list((self.root / 'build').rglob('*.gn'))), 4)

    def test_explicit_gn_template_is_used_and_mismatched_os_rejected(self):
        custom = self.root / 'build/mac/custom.gn'
        custom.write_text((self.root / 'build/mac/args.gn').read_text() + '\nsymbol_level = 1\n')
        self.assertEqual(build.args_template('mac', custom), custom.resolve())
        self.assertIn('symbol_level = 1', build.render_args(custom, 'mac', 'arm64', 'dynamic'))
        with self.assertRaises(RuntimeError):
            build.render_args(custom, 'linux', 'x64', 'static')
        outside = self.root / 'outside.gn'
        outside.write_text('target_os = "mac"')
        with self.assertRaises(RuntimeError):
            build.args_template('mac', outside)

    def test_gn_overrides_remain_before_dependent_expressions(self):
        path = self.root / 'build/mac/args.gn'
        path.write_text('target_os = "mac"\ntarget_cpu = "x64"\n'
                        'is_component_build = false\n'
                        'if (target_cpu == "arm64") { symbol_level = 1 }\n'
                        'if (is_component_build) { dcheck_always_on = true }\n')
        rendered = build.render_args(path, 'mac', 'arm64', 'dynamic')
        self.assertLess(rendered.index('target_cpu = "arm64"'), rendered.index('if (target_cpu'))
        self.assertLess(rendered.index('is_component_build = true'), rendered.index('if (is_component_build)'))
        path.write_text(path.read_text() + 'target_cpu = "x86"\n')
        with self.assertRaises(RuntimeError):
            build.render_args(path, 'mac', 'arm64', 'static')

    def test_failed_gn_gen_invalidates_previous_success_marker(self):
        out = self.src / f'out/arupa-mac-arm64-{VERSION}-static'
        out.mkdir(parents=True)
        (out / 'build.ninja').write_text('old graph')
        stamp = out / 'arupa-build.json'
        stamp.write_text('{"old": true}')
        for name in ('buildtools/mac/gn', 'third_party/ninja/ninja'):
            tool = self.src / name
            tool.parent.mkdir(parents=True, exist_ok=True)
            tool.touch()
        arguments = ['desktop', '--os', 'mac', '--arch', 'arm64']
        with patch.object(fetch, 'HOST_OS', 'mac'), patch.object(build, 'prepare_project'), patch.object(build, 'prepare_mac_toolchain'):
            with patch.object(fetch, 'run', side_effect=RuntimeError('GN failed')):
                with self.assertRaisesRegex(RuntimeError, 'GN failed'):
                    self.quiet(build.main, [*arguments[:1], 'gen', *arguments[1:]])
            self.assertFalse(stamp.exists())
            with patch.object(fetch, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, '请先执行 gen'):
                    self.quiet(build.main, [*arguments[:1], 'build', *arguments[1:]])
                run.assert_not_called()

    def test_mac_toolchain_rejects_clt_before_build_writes(self):
        (self.root / '.env').write_text('chromium_developer_dir=/Library/Developer/CommandLineTools\n')
        with patch.object(fetch, 'HOST_OS', 'mac'), patch.object(build, 'prepare_project') as prepare:
            with self.assertRaisesRegex(RuntimeError, 'Command Line Tools'):
                self.quiet(build.main, ['desktop', 'build', '--os', 'mac', '--arch', 'x64'])
        prepare.assert_not_called()
        self.assertFalse((self.src / 'out').exists())

    def test_mac_toolchain_respects_explicit_environment_and_checks_sdk(self):
        developer = self.root / 'Xcode.app/Contents/Developer'
        (developer / 'usr/bin').mkdir(parents=True)
        (developer / 'usr/bin/xcodebuild').touch()
        with patch.dict(os.environ, {'DEVELOPER_DIR': str(developer)}), patch('subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout='ok', stderr='')
            self.quiet(build.prepare_mac_toolchain, {})
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.kwargs['env']['DEVELOPER_DIR'], str(developer))
            self.assertEqual(run.call_args.args[0], ['/usr/bin/xcrun', '--sdk', 'macosx', 'metal', '--version'])
            run.return_value = subprocess.CompletedProcess([], 1, stdout='', stderr='SDK missing')
            with self.assertRaisesRegex(RuntimeError, 'SDK missing'):
                build.prepare_mac_toolchain({})
            run.side_effect = [subprocess.CompletedProcess([], 0, 'Xcode', ''),
                               subprocess.CompletedProcess([], 0, '/SDK', ''),
                               subprocess.CompletedProcess([], 1, '', 'missing Metal Toolchain')]
            with self.assertRaisesRegex(RuntimeError, '-downloadComponent MetalToolchain'):
                build.prepare_mac_toolchain({})

    def test_mac_toolchain_auto_selects_unique_full_xcode(self):
        app = self.root / 'Xcode.app'
        developer = app / 'Contents/Developer'
        (developer / 'usr/bin').mkdir(parents=True)
        (developer / 'usr/bin/xcodebuild').touch()
        with patch.object(Path, 'glob', return_value=[app]), patch('subprocess.run') as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, '/Library/Developer/CommandLineTools\n', ''),
                               subprocess.CompletedProcess([], 0, 'Xcode', ''),
                               subprocess.CompletedProcess([], 0, '/SDK', ''),
                               subprocess.CompletedProcess([], 0, 'metal version', '')]
            self.quiet(build.prepare_mac_toolchain, {})
            self.assertEqual(os.environ['DEVELOPER_DIR'], str(developer))

    def test_atomic_config_write_failure_preserves_previous_file(self):
        target = self.root / 'args.gn'
        target.write_text('original')
        with patch.object(build.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.quiet(build.write_if_changed, target, 'new')
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(list(self.root.glob('.args.gn.*')), [])

    def test_fetch_target_os_is_merged_for_each_platform(self):
        path = self.root / '.gclient'
        for target in ('win', 'mac', 'linux', 'android'):
            with patch.object(fetch, 'HOST_OS', 'mac'):
                self.quiet(fetch.write_gclient, 'url', target == 'android', target)
            scope = {}
            exec(path.read_text(), scope)
            self.assertIn(target, scope['target_os'])
            self.assertIn('mac', scope['target_os'])
            self.assertEqual(len(scope['target_os']), len(set(scope['target_os'])))

    def test_android_snapshots_override_template_per_arch(self):
        path = self.root / 'build/android/custom.gn'
        for existing in ('true', 'false', None):
            text = 'target_os = "android"\nis_component_build = true\n'
            if existing is not None:
                text += f'include_both_v8_snapshots = {existing}\n'
            path.write_text(text)
            for cpu, expected in (('arm64', 'true'), ('x64', 'false')):
                result = build.render_args(path, 'android', cpu, 'static')
                self.assertIn('is_component_build = false', result)
                self.assertIn(f'include_both_v8_snapshots = {expected}', result)
                self.assertEqual(result.count('include_both_v8_snapshots ='), 1)
                self.assertEqual(path.read_text(), text)

    def test_android_dynamic_rejected_for_all_actions_and_renderer(self):
        for action in ('gen', 'build', 'package', 'all'):
            with self.subTest(action=action), patch.object(build, 'prepare_project') as prepare:
                with self.assertRaises(SystemExit):
                    self.quiet(build.main, ['android', action, '--link', 'dynamic', '--dry-run'])
                prepare.assert_not_called()
        with self.assertRaises(RuntimeError):
            build.render_args(self.root / 'build/android/args.gn', 'android', 'arm64', 'dynamic')

    def test_android_all_writes_distinct_snapshot_settings(self):
        original = (self.root / 'build/android/args.gn').read_bytes()
        for name in ('buildtools/linux64/gn', 'third_party/ninja/ninja'):
            path = self.src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        with patch.object(fetch, 'HOST_OS', 'linux'), patch.object(build, 'prepare_project'), patch.object(fetch, 'run'):
            self.quiet(build.main, ['android', 'gen', '--arch', 'all',
                                  '--args', str(self.root / 'build/android/args.gn')])
        for cpu, value in (('arm64', 'true'), ('x64', 'false')):
            args_file = self.src / f'out/arupa-android-{cpu}-{VERSION}-static/args.gn'
            self.assertIn(f'include_both_v8_snapshots = {value}', args_file.read_text())
            self.assertIn('is_component_build = false', args_file.read_text())
        self.assertEqual((self.root / 'build/android/args.gn').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
