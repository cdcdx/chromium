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
        # DEPS 已同步的空清单：verify_deps 只据此判断独立仓库是否为空。
        (self.root / '.gclient_entries').write_text('')
        for module, name, value in ((fetch, 'WORKSPACE_ROOT', self.root), (fetch, 'CHROMIUM_SRC', self.src),
                                    (build, 'ROOT', self.root), (build, 'SRC', self.src), (fetch, 'DRY_RUN', False)):
            obj = patch.object(module, name, value)
            obj.start()
            self.addCleanup(obj.stop)
        env = patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def quiet(self, func, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return func(*args, **kwargs)

    def test_windows_build_refreshes_facade_even_with_existing_dll(self):
        import nomad
        from types import SimpleNamespace
        repo = self.root / 'nomad_desktop'
        project = repo / 'NomadBrowser.Avalonia/NomadBrowser.Avalonia.csproj'
        project.parent.mkdir(parents=True)
        project.write_text('<Project />')
        for name in ('NomadBrowser.Updater', 'NomadBrowser.Windows.Updater'):
            folder = repo / name
            folder.mkdir()
            (folder / (name + '.csproj')).write_text('<Project />')
        kernel = self.root / 'delivery'
        sdk = kernel / 'dotnet'
        sdk.mkdir(parents=True)
        facade = sdk / 'ArupaKernel.csproj'
        facade.write_text('<Project />')
        (sdk / 'ArupaKernel.dll').write_bytes(b'old SDK without OpenDevTools')
        args = SimpleNamespace(pc_project=None, nuget_config=self.root / 'build/nuget.config',
                               variant='release', no_web=True)
        with patch.object(fetch, 'DRY_RUN', True), patch.object(nomad, 'delivery', return_value=kernel), \
             patch.object(nomad, 'prepare_dotnet', return_value='dotnet'), patch.object(fetch, 'run') as run:
            self.quiet(nomad.desktop_build, self.root, args, 'win', 'x64', VERSION, self.root / 'output')
            builds = [call.args[0] for call in run.call_args_list if call.args[0][1] == 'build']
            self.assertEqual(len(builds), 1)
            self.assertEqual(builds[0][2], facade)
            self.assertEqual(builds[0][builds[0].index('-o') + 1], sdk)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS extended attributes')
    def test_mac_payload_copy_preserves_signature_attributes_and_symlinks(self):
        import nomad
        source = self.root / 'signed app'
        source.mkdir()
        payload = source / 'managed.dll'
        payload.write_bytes(b'managed payload')
        (source / 'link').symlink_to('managed.dll')
        subprocess.run(['xattr', '-w', 'com.nomad.signature-test', 'preserved', str(payload)], check=True)
        destination = self.root / 'copied app'
        self.quiet(nomad.copy_payload_tree, source, destination, 'mac')
        self.assertEqual(subprocess.check_output(['xattr', '-p', 'com.nomad.signature-test',
            str(destination / 'managed.dll')]).strip(), b'preserved')
        self.assertTrue((destination / 'link').is_symlink())

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

    def test_every_desktop_platform_builds_subprocess_helper(self):
        for target_os in ('win', 'mac', 'linux'):
            with self.subTest(target_os=target_os):
                self.assertIn('//chrome/browser/arupa_desktop:render',
                              build.build_targets('arupa_desktop', target_os))
        with patch.object(build, 'exclude_generated'):
            self.quiet(build.prepare_project, 'arupa_desktop')
        graph = (self.src / 'arupa_build/arupa_desktop/BUILD.gn').read_text()
        self.assertIn('if (is_win || is_mac || is_linux) { deps += [ "//chrome/browser/arupa_desktop:render" ] }', graph)

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

    def test_android_alias_selects_android_host_dependencies(self):
        import native_tools
        with patch.object(fetch, 'HOST_OS', 'linux'), \
             patch.object(fetch, 'setup_depot_tools'), \
             patch.object(fetch, 'fetch_chromium', return_value='url'), \
             patch.object(fetch, 'write_gclient') as gclient, \
             patch.object(fetch, 'sync_deps') as sync, \
             patch.object(native_tools, 'setup_host') as host:
            self.quiet(fetch.main, ['android', '--ver', VERSION, '--install-host-deps'])
        self.assertEqual(host.call_args.args[:2], ('android', ('x64', 'arm64')))
        gclient.assert_called_once_with('url', True, 'android')
        self.assertTrue(sync.call_args.args[2])
        self.assertEqual(sync.call_args.args[-1], 'android')

    def test_toolchain_conflicts_fail_before_download(self):
        for targets in (['toolchains', '--nohooks'], ['android-sdk', '--nohooks'],
                        ['jdk', '--nohooks'], ['metal']):
            with self.subTest(targets=targets), patch.object(fetch, 'HOST_OS', 'linux'), \
                 patch.object(fetch, 'setup_depot_tools') as download, \
                 patch.object(fetch, 'apply_proxy') as proxy:
                with self.assertRaises(SystemExit):
                    self.quiet(fetch.main, ['depot_tools', *targets, '--ver', VERSION])
                download.assert_not_called()
                proxy.assert_not_called()
        with patch.object(fetch, 'sync_deps') as sync:
            self.quiet(fetch.main, ['deps', '--nohooks', '--ver', VERSION])
        self.assertTrue(sync.call_args.args[4])

    def test_platform_aliases_are_shared_by_entrypoints(self):
        import backup
        for alias, canonical in (('windows', 'win'), ('macos', 'mac')):
            for module, positional in ((fetch, ['deps']), (build, ['desktop']),
                                       (backup, ['--ver', VERSION])):
                with self.subTest(module=module.__name__, alias=alias):
                    args = module.build_parser().parse_args([*positional, '--os', alias])
                    self.assertEqual(args.os, canonical)

    def test_xcode_app_path_normalized_in_dry_run_without_processes(self):
        import apple_tools
        import toolchains
        with patch.object(fetch, 'DRY_RUN', True), \
             patch('subprocess.run', side_effect=AssertionError('unexpected process')):
            cfg = {'chromium_developer_dir': '/Applications/Custom Xcode.app'}
            expected = Path('/Applications/Custom Xcode.app/Contents/Developer')
            self.assertEqual(toolchains.xcode_directory(cfg), expected)
            self.assertEqual(apple_tools.xcode_directory(cfg), expected)
            self.quiet(build.prepare_mac_toolchain, cfg)

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

    def flaky_run(self, body, attempts, *, retry=True, error=None, **kwargs):
        """执行一段按次数记录尝试的 Python 片段，返回 (尝试次数, sleep 次数)。"""
        counter = self.root / 'attempts'
        counter.unlink(missing_ok=True)
        code = ('import pathlib, sys\n'
                'counter = pathlib.Path(sys.argv[1])\n'
                'attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n'
                'counter.write_text(str(attempt))\n'
                + body)
        command = [sys.executable, '-c', code, str(counter)]
        with patch.object(fetch, 'RETRIES', attempts), patch.object(fetch, 'RETRY_DELAY', 0), \
             patch.object(fetch, 'time') as clock:
            if error is None:
                self.quiet(fetch.run, command, self.root, False, retry, **kwargs)
            else:
                with self.assertRaisesRegex(*error):
                    self.quiet(fetch.run, command, self.root, False, retry, **kwargs)
            return counter.read_text(), clock.sleep.call_count

    def test_dirty_checkout_lists_the_offending_entries(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'local-change.txt').write_text('x')
        with self.assertRaisesRegex(RuntimeError, 'local-change.txt'):
            self.quiet(fetch.require_clean, repo)

    def test_depot_bootstrap_artifacts_are_excluded_from_the_clean_check(self):
        # depot_tools 自举生成的未跟踪文件不该被判定成"本地改动"。
        repo = self.root / 'depot'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'python3_bin_reldir.txt').write_text('bootstrap-2@3.11.8.chromium.35_bin/python3/bin\n')
        (repo / 'bootstrap-2@3.11.8.chromium.35_bin').mkdir()
        with self.assertRaises(RuntimeError):
            self.quiet(fetch.require_clean, repo)
        self.quiet(fetch.exclude_paths, repo, fetch.DEPOT_GENERATED, 'depot_tools 自举产物')
        self.quiet(fetch.require_clean, repo)
        exclude = (repo / '.git/info/exclude').read_text()
        self.assertIn('/python3_bin_reldir.txt', exclude)
        self.assertIn('/bootstrap-*_bin', exclude)

    def test_python3_resolution_follows_the_bootstrap_manifest(self):
        depot = self.root / 'depot_tools'
        interpreter = depot / 'bootstrap-2@3.11.8.chromium.35_bin/python3/bin'
        interpreter.mkdir(parents=True)
        (interpreter / 'python3').write_text('#!/bin/sh\n')
        (depot / 'python3_bin_reldir.txt').write_text('bootstrap-2@3.11.8.chromium.35_bin/python3/bin\n')
        self.assertEqual(fetch.python3_candidates(depot)[0], interpreter)
        # gclient/gsutil/ensure_bootstrap 执行的是 python（自举目录里只有 python3）。
        self.assertEqual(fetch.ensure_python_alias(interpreter), interpreter)
        self.assertEqual(os.readlink(interpreter / 'python'), 'python3')
        with patch.dict(os.environ, {'PATH': '/usr/bin'}):
            self.quiet(fetch.ensure_real_python3, depot)
            self.assertEqual(os.environ['PATH'].split(os.pathsep)[0], str(interpreter))

    def test_depot_tools_bootstrap_uses_the_script_that_exists(self):
        depot = self.root / 'depot_tools'
        (depot / '.git').mkdir(parents=True)
        (depot / 'ensure_bootstrap').write_text('#!/bin/bash\n')
        with patch.object(fetch, 'exclude_paths') as exclude, patch.object(fetch, 'require_clean'), \
             patch.object(fetch, 'tool_environment'), patch.object(fetch, 'run') as run:
            self.quiet(fetch.setup_depot_tools, {})
        exclude.assert_called_once_with(depot, fetch.DEPOT_GENERATED, 'depot_tools 自举产物')
        commands = [list(map(str, call.args[0])) for call in run.call_args_list]
        self.assertIn(['bash', str(depot / 'ensure_bootstrap')], commands)
        self.assertNotIn('bootstrap_python3', ' '.join(' '.join(c) for c in commands))
        (depot / 'bootstrap_python3').write_text('#!/bin/bash\n')
        with patch.object(fetch, 'exclude_paths'), patch.object(fetch, 'require_clean'), \
             patch.object(fetch, 'tool_environment'), patch.object(fetch, 'run') as run:
            self.quiet(fetch.setup_depot_tools, {})
        commands = [' '.join(map(str, call.args[0])) for call in run.call_args_list]
        self.assertTrue(any('bootstrap_python3' in command for command in commands))

    def test_per_call_retry_override_is_honoured(self):
        # 大仓补拉用更多次数：RETRIES=1 时仍按 retries=3 尝试 4 次。
        body = 'print("fatal: early EOF", file=sys.stderr)\nsys.exit(128)\n'
        failure = (subprocess.CalledProcessError, '')
        self.assertEqual(self.flaky_run(body, 1, retries=3, error=failure), ('4', 3))

    def test_transient_rate_limit_is_retried_then_succeeds(self):
        body = ('if attempt < 3:\n'
                '    print("remote: RESOURCE_EXHAUSTED: Short term server-time rate limit exceeded", file=sys.stderr)\n'
                '    print("fatal: unable to access: The requested URL returned error: 429", file=sys.stderr)\n'
                '    sys.exit(128)\n')
        self.assertEqual(self.flaky_run(body, 3), ('3', 2))

    def test_rate_limit_exhaustion_points_at_mirror_and_proxy(self):
        body = ('print("remote: RESOURCE_EXHAUSTED: Short term server-time rate limit exceeded", file=sys.stderr)\n'
                'print("fatal: unable to access: The requested URL returned error: 429", file=sys.stderr)\n'
                'sys.exit(128)\n')
        error = (RuntimeError, 'chromium_mirror=1.*https_proxy')
        self.assertEqual(self.flaky_run(body, 2, error=error), ('3', 2))

    def test_transient_failure_reported_on_stdout_is_also_retried(self):
        # gclient 把 git 的 429 打到 stdout —— 只看 stderr 会漏判。
        body = ('if attempt < 2:\n'
                '    print("fatal: unable to access: The requested URL returned error: 429")\n'
                '    sys.exit(1)\n')
        self.assertEqual(self.flaky_run(body, 3), ('2', 1))

    def test_long_silent_command_reports_a_heartbeat(self):
        # gclient/vpython 会把子进程输出缓存到结束才打印：静默几分钟时必须让用户知道还活着。
        silent = [sys.executable, '-c', 'import time; time.sleep(0.6)']
        output = io.StringIO()
        with patch.object(fetch, 'HEARTBEAT_SECONDS', 0.05), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(io.StringIO()):
            fetch.run(silent, self.root, False, True)
        self.assertIn('仍在运行', output.getvalue())

    def test_permanent_git_error_is_not_retried(self):
        body = 'print("fatal: reference is not a tree: deadbeef", file=sys.stderr)\nsys.exit(128)\n'
        failure = subprocess.CalledProcessError
        self.assertEqual(self.flaky_run(body, fetch.DEFAULT_RETRIES, error=(failure, '')), ('1', 0))
        self.assertEqual(self.flaky_run(body, fetch.DEFAULT_RETRIES, retry=False, error=(failure, '')), ('1', 0))

    def test_existing_local_tag_skips_network_fetch(self):
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
        checkout = self.root / 'checkout'
        with patch.object(fetch, 'CHROMIUM_SRC', checkout):
            self.quiet(fetch.fetch_chromium, {'chromium_src': str(remote)}, VERSION, True)
            calls = []
            def fake(cmd, cwd=None, capture=False, retry=False):
                calls.append([str(item) for item in cmd])
                return ''
            with patch.object(fetch, 'run', side_effect=fake):
                self.quiet(fetch.fetch_chromium, {'chromium_src': str(remote)}, VERSION, True)
        self.assertFalse([cmd for cmd in calls if 'fetch' in cmd])
        self.assertEqual(calls[-1][1:3], ['checkout', '--detach'])

    def test_unsynced_deps_warn_but_do_not_block_the_build(self):
        # 有些空仓库是 ANGLE/Dawn 的测试数据，跟本次目标无关，不该挡住编译。
        (self.root / '.gclient_entries').write_text(
            "  'src/third_party/skia': 'https://skia.googlesource.com/skia.git@a9c42c9f',\n")
        (self.src / 'third_party/skia/.git').mkdir(parents=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            build.verify_deps(VERSION)
        message = output.getvalue()
        self.assertIn('third_party/skia', message)
        self.assertIn('skia.googlesource.com', message)
        self.assertIn(f'fetch.sh deps --ver {VERSION}', message)

    def test_gn_load_failure_names_the_unsynced_dependency(self):
        (self.root / '.gclient_entries').write_text(
            "  'src/third_party/skia': 'https://skia.googlesource.com/skia.git@a9c42c9f',\n")
        output = (f'ERROR at //skia/BUILD.gn:16:1: Unable to load '
                  f'"{self.src}/third_party/skia/gn/shared_sources.gni".\n'
                  'import("//third_party/skia/gn/shared_sources.gni")\n')
        hint = build.gn_failure_hint(output, VERSION)
        self.assertIn('src/third_party/skia', hint)
        self.assertIn('a9c42c9f', hint)
        self.assertIn(f'fetch.sh deps --ver {VERSION}', hint)
        # 与 DEPS 无关的 GN 错误不能改写成"依赖没同步"，必须原样抛给用户。
        self.assertIsNone(build.gn_failure_hint('ERROR at //foo/BUILD.gn:1:1: syntax error', VERSION))
        self.assertIsNone(build.gn_failure_hint('', VERSION))

    def test_deps_check_ignores_non_git_and_requires_sync_record(self):
        (self.root / '.gclient_entries').write_text("  'src/third_party/test_fonts/test_fonts': 'gs://chromium-fonts/abc',\n")
        self.quiet(build.verify_deps, VERSION)
        (self.root / '.gclient_entries').unlink()
        with self.assertRaisesRegex(RuntimeError, 'fetch.sh deps'):
            self.quiet(build.verify_deps, VERSION)

    def test_repair_deps_fetches_repos_left_empty_by_gclient(self):
        remote = self.root / 'dep-remote'
        remote.mkdir()
        def git(*args, cwd=remote):
            return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        git('init')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.invalid')
        (remote / 'a.txt').write_text('content')
        git('add', '.')
        git('commit', '-m', 'one')
        git('tag', 'v1')
        sha = git('rev-parse', 'HEAD')
        (self.root / '.gclient_entries').write_text(f"  'src/third_party/broken': '{remote}@refs/tags/v1',\n")
        dep = self.src / 'third_party/broken'
        dep.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(dep)], check=True)
        pack = dep / '.git/objects/pack'
        pack.mkdir(parents=True, exist_ok=True)
        (pack / 'tmp_pack_stale').write_bytes(b'partial download')
        self.assertEqual(self.quiet(fetch.repair_deps), ['src/third_party/broken'])
        self.assertEqual((dep / 'a.txt').read_text(), 'content')
        self.assertEqual(git('-C', str(dep), 'rev-parse', 'HEAD', cwd=self.root), sha)
        self.assertEqual(list(pack.glob('tmp_pack_*')), [])

    def test_repair_deps_requires_the_gclient_manifest(self):
        (self.root / '.gclient_entries').unlink()
        with self.assertRaisesRegex(RuntimeError, 'gclient sync'):
            self.quiet(fetch.repair_deps)

    def test_invalid_retry_options_fail_before_network(self):
        for extra in (['--retries', '-1'], ['--retry-delay', '-1']):
            with self.subTest(extra=extra), patch.object(fetch, 'run', side_effect=AssertionError('network')):
                with self.assertRaises(SystemExit):
                    self.quiet(fetch.main, ['depot_tools', *extra, '--ver', VERSION])

    def package_fixture(self):
        scripts = self.root / 'scripts/packaging'
        scripts.mkdir(parents=True)
        for name in ('desktop', 'android'):
            shutil.copy(REPO / f'scripts/packaging/package-arupa_{name}.sh', scripts)
        for platform_name in ('desktop', 'android'):
            attachments = self.root / f'package/package_{platform_name}'
            (attachments / 'docs').mkdir(parents=True)
            (attachments / 'docs/readme.txt').write_text(platform_name)
            (attachments / '.hidden').write_text('hidden attachment')
            (attachments / 'plugin-runtime').mkdir()
            (attachments / 'plugin-runtime/runtime.js').write_text('ordinary attachment')
        out = self.root / 'output'
        out.mkdir()
        (out / 'hyphen-data').mkdir()
        (out / 'hyphen-data/manifest.json').write_text('{"manifest_version":2}')
        (out / 'hyphen-data/hyph-en-us.hyb').write_bytes(b'fixture')
        for name in ('content_shell.pak', 'devtools_resources.pak', 'icudtl.dat', 'snapshot_blob.bin', 'arupa_render', 'arupa_plugin_host'):
            (out / name).write_bytes(b'fixture')
        for name in ('gen/extensions/strings/extensions_strings_en-US.pak',
                     'gen/extensions/extensions_renderer_generated_resources.pak',
                     'gen/components/test resources/fixture.pak'):
            resource = out / name
            resource.parent.mkdir(parents=True, exist_ok=True)
            resource.write_bytes(name.encode())
        return out

    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'),
                         'POSIX package integration')
    def test_linux_package_rejects_missing_subprocess_helper(self):
        out = self.package_fixture()
        (out / 'libarupa_kernel.so').write_bytes(b'fixture')
        (out / 'arupa_render').unlink()
        result = subprocess.run(['bash', str(self.root / 'scripts/packaging/package-arupa_desktop.sh'),
                                 '--os', 'linux', '--arch', 'x64', '--ver', VERSION, '--out', str(out)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('arupa_render', result.stderr + result.stdout)
        self.assertFalse((self.root / f'dist/arupa-linux-x64-{VERSION}-static-1').exists())

    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'),
                         'POSIX package integration')
    def test_package_rejects_missing_devtools_resources(self):
        out = self.package_fixture()
        (out / 'libarupa_kernel.so').write_bytes(b'fixture')
        (out / 'devtools_resources.pak').unlink()
        result = subprocess.run(['bash', str(self.root / 'scripts/packaging/package-arupa_desktop.sh'),
                                 '--os', 'linux', '--arch', 'x64', '--ver', VERSION, '--out', str(out)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('devtools_resources.pak', result.stderr + result.stdout)
        self.assertFalse((self.root / f'dist/arupa-linux-x64-{VERSION}-static-1').exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS package integration')
    def test_mac_package_rejects_missing_extension_resources(self):
        out = self.package_fixture()
        (out / 'libarupa_kernel.dylib').write_bytes(struct.pack('<IiiIIIII', 0xfeedfacf, 0x100000c, 0, 6, 0, 0, 0, 0))
        for relative in ('gen/extensions/strings/extensions_strings_en-US.pak',
                         'gen/extensions/extensions_renderer_generated_resources.pak'):
            path = out / relative
            content = path.read_bytes()
            path.unlink()
            result = subprocess.run(['bash', str(self.root / 'scripts/packaging/package-arupa_desktop.sh'),
                                     '--os', 'mac', '--arch', 'arm64', '--ver', VERSION, '--out', str(out)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(relative, result.stderr + result.stdout)
            self.assertFalse((self.root / f'dist/arupa-mac-arm64-{VERSION}-static-1').exists())
            path.write_bytes(content)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS package integration')
    def test_mac_package_rejects_missing_hyphen_data(self):
        out = self.package_fixture()
        (out / 'libarupa_kernel.dylib').write_bytes(struct.pack('<IiiIIIII', 0xfeedfacf, 0x100000c, 0, 6, 0, 0, 0, 0))
        for missing in ('manifest.json', 'hyph-en-us.hyb'):
            path = out / 'hyphen-data' / missing
            content = path.read_bytes()
            path.unlink()
            result = subprocess.run(['bash', str(self.root / 'scripts/packaging/package-arupa_desktop.sh'),
                                     '--os', 'mac', '--arch', 'arm64', '--ver', VERSION, '--out', str(out)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('hyphen-data', result.stderr + result.stdout)
            self.assertFalse((self.root / f'dist/arupa-mac-arm64-{VERSION}-static-1').exists())
            path.write_bytes(content)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS package integration')
    def test_desktop_package_and_arch_mismatch_cleanup(self):
        out = self.package_fixture()
        # Minimal valid Mach-O header: file(1) can identify the architecture.
        (out / 'libarupa_kernel.dylib').write_bytes(struct.pack('<IiiIIIII', 0xfeedfacf, 0x100000c, 0, 6, 0, 0, 0, 0))
        command = ['bash', str(self.root / 'scripts/packaging/package-arupa_desktop.sh'),
                   '--os', 'mac', '--arch', 'arm64', '--ver', VERSION,
                   '--out', str(out), '--zip']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        delivery = self.root / f'dist/arupa-mac-arm64-{VERSION}-static-1'
        self.assertTrue((delivery / 'SHA256SUMS.txt').is_file())
        self.assertTrue((delivery / 'MANIFEST.md').is_file())
        self.assertTrue(Path(str(delivery) + '.zip').is_file())
        self.assertEqual((delivery / 'docs/readme.txt').read_text(), 'desktop')
        self.assertTrue((delivery / '.hidden').is_file())
        self.assertTrue((delivery / 'kernel/plugin-runtime/runtime.js').is_file())
        self.assertFalse((delivery / 'plugin-runtime').exists())
        for resource in (out / 'gen').rglob('*.pak'):
            relative = resource.relative_to(out)
            self.assertEqual((delivery / 'kernel' / relative).read_bytes(), resource.read_bytes())
            self.assertIn('kernel/' + relative.as_posix(), (delivery / 'SHA256SUMS.txt').read_text())
        self.assertFalse((delivery / 'package_android').exists())
        self.assertIn('docs/readme.txt', (delivery / 'SHA256SUMS.txt').read_text())
        with zipfile.ZipFile(str(delivery) + '.zip') as archive:
            self.assertTrue(any(name.endswith('/docs/readme.txt') for name in archive.namelist()))
        command[command.index('arm64')] = 'x64'
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / f'dist/arupa-mac-x64-{VERSION}-static-1').exists())
        self.assertTrue(delivery.exists())

    def test_android_wrong_abi_removes_partial_delivery(self):
        out = self.package_fixture()
        (out / 'apks').mkdir()
        with zipfile.ZipFile(out / 'apks/arupa-kernel.aar', 'w') as aar:
            aar.writestr('jni/x86_64/libarupakernel.so', b'fixture')
        apk = out / 'gen/chrome/browser/arupa_android/aar/arupa_kernel_resources.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'fixture')
        command = ['bash', str(self.root / 'scripts/packaging/package-arupa_android.sh'),
                   '--arch', 'arm64', '--ver', VERSION, '--out', str(out),
                   '--no-package']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('jni', result.stdout + result.stderr)
        self.assertFalse((self.root / f'dist/arupa-android-{VERSION}-static-1').exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Android packaging requires Linux GNU tools')
    def test_android_package_includes_only_android_attachments(self):
        out = self.package_fixture()
        (out / 'apks').mkdir()
        with zipfile.ZipFile(out / 'apks/arupa-kernel.aar', 'w') as aar:
            aar.writestr('jni/arm64-v8a/libarupakernel.so', b'fixture')
        apk = out / 'gen/chrome/browser/arupa_android/aar/arupa_kernel_resources.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'fixture')
        result = subprocess.run(['bash', str(self.root / 'scripts/packaging/package-arupa_android.sh'),
                                 '--arch', 'arm64', '--ver', VERSION, '--out', str(out)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        delivery = self.root / f'dist/arupa-android-{VERSION}-static-1'
        self.assertEqual((delivery / 'docs/readme.txt').read_text(), 'android')
        self.assertTrue((delivery / '.hidden').is_file())
        self.assertTrue((delivery / 'plugin-runtime/runtime.js').is_file())
        self.assertFalse((delivery / 'kernel/arm64/plugin-runtime').exists())
        self.assertFalse((delivery / 'package_desktop').exists())
        self.assertIn('docs/readme.txt', (delivery / 'SHA256SUMS.txt').read_text())

    def test_package_requires_platform_attachment_directory_before_commands(self):
        with patch.object(fetch, 'HOST_OS', 'mac'), patch.object(fetch, 'run', side_effect=AssertionError('executed')):
            with self.assertRaisesRegex(RuntimeError, 'package_desktop'):
                self.quiet(build.main, ['desktop', 'package', '--os', 'mac', '--arch', 'x64'])

    def test_missing_project_config_fails_before_any_fetch(self):
        with patch.object(fetch, 'run', side_effect=AssertionError('network before validation')):
            with self.assertRaises(SystemExit):
                self.quiet(fetch.main, ['all', '--ver', VERSION])

    def test_project_ref_and_path_are_not_shared(self):
        calls = []
        with patch.object(fetch, 'fetch_project', side_effect=lambda *args: calls.append(args)):
            self.quiet(fetch.main, ['arupa_desktop', 'nomad_android',
                                   '--arupa-desktop-src', 'desktop-url', '--arupa-desktop-ver', 'v2',
                                   '--nomad-android-src', 'android-url', '--nomad-android-ver', 'refs/heads/release'])
        self.assertEqual(calls, [('arupa_desktop', 'desktop-url', 'v2'),
                                 ('nomad_android', 'android-url', 'refs/heads/release')])

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

    def test_gn_load_failure_on_stdout_is_translated_with_the_dependency(self):
        (self.root / '.gclient_entries').write_text(
            "  'src/third_party/skia': 'https://skia.googlesource.com/skia.git@a9c42c9f',\n")
        for name in ('buildtools/mac/gn', 'third_party/ninja/ninja'):
            tool = self.src / name
            tool.parent.mkdir(parents=True, exist_ok=True)
            tool.touch()
        # GN 把错误写在 stdout：只读 stderr 会退化成裸的 "Command ... returned non-zero exit status 1"。
        failure = subprocess.CalledProcessError(
            1, ['gn'], output=f'Unable to load "{self.src}/third_party/skia/gn/shared_sources.gni"', stderr='')
        with patch.object(fetch, 'HOST_OS', 'mac'), patch.object(build, 'prepare_project'), \
             patch.object(build, 'prepare_mac_toolchain'), patch.object(build.native_tools, 'kernel_check'), \
             patch.object(fetch, 'run', side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, 'src/third_party/skia') as caught:
                self.quiet(build.main, ['desktop', 'gen', '--os', 'mac', '--arch', 'x64'])
        self.assertIn(f'fetch.sh deps --ver {VERSION}', str(caught.exception))

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
        with patch.object(fetch, 'HOST_OS', 'mac'), patch.object(build, 'prepare_project'), patch.object(build, 'prepare_mac_toolchain'), patch.object(build.native_tools, 'kernel_check'):
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
        with patch.object(fetch, 'HOST_OS', 'linux'), patch.object(build, 'prepare_project'), patch.object(fetch, 'run'), patch.object(build.native_tools, 'kernel_check'):
            self.quiet(build.main, ['android', 'gen', '--arch', 'all',
                                  '--args', str(self.root / 'build/android/args.gn')])
        for cpu, value in (('arm64', 'true'), ('x64', 'false')):
            args_file = self.src / f'out/arupa-android-{cpu}-{VERSION}-static/args.gn'
            self.assertIn(f'include_both_v8_snapshots = {value}', args_file.read_text())
            self.assertIn('is_component_build = false', args_file.read_text())
        self.assertEqual((self.root / 'build/android/args.gn').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
