"""Toolchain bootstrap regression tests: no downloads or machine changes."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import ssl
import urllib.error
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fetch
import toolchains


class ToolchainsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, key, value in ((fetch, 'WORKSPACE_ROOT', self.root),
                                   (fetch, 'CHROMIUM_SRC', self.root / 'src'),
                                   (fetch, 'DRY_RUN', False), (fetch, 'HOST_OS', 'mac'), (fetch, 'IS_WIN', False)):
            setting = patch.object(module, key, value)
            setting.start()
            self.addCleanup(setting.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def quiet(self, function, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args)

    def test_fetch_toolchain_dry_run_is_read_only(self):
        with patch('subprocess.run', side_effect=AssertionError('executed')), patch('urllib.request.urlopen', side_effect=AssertionError('downloaded')):
            self.quiet(fetch.main, ['metal', 'dotnet', '--dry-run'])
            with patch.object(fetch, 'HOST_OS', 'linux'):
                self.quiet(fetch.main, ['android-sdk', 'jdk', '--dry-run'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_metal_idempotent_and_missing_component_downloaded(self):
        developer = self.root / 'Xcode.app/Contents/Developer'
        (developer / 'usr/bin').mkdir(parents=True)
        (developer / 'usr/bin/xcodebuild').touch()
        cfg = {'chromium_developer_dir': str(developer)}
        with patch('subprocess.run') as probe, patch.object(fetch, 'run') as run:
            probe.return_value = subprocess.CompletedProcess([], 0, 'metal version', '')
            self.quiet(toolchains.setup_metal, cfg)
            run.assert_not_called()
            probe.return_value = subprocess.CompletedProcess([], 1, '', 'missing')
            toolchains.setup_metal(cfg)
            self.assertIn('-downloadComponent', run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[-1].args[0][-2:], ['metal', '--version'])
            run.side_effect = RuntimeError('download failed')
            with self.assertRaisesRegex(RuntimeError, 'download failed'):
                toolchains.setup_metal(cfg)

    def test_dotnet_install_honors_global_json_and_local_directory(self):
        repo = self.root / 'nomadbrowser.pc'
        repo.mkdir()
        (repo / 'global.json').write_text(json.dumps({'sdk': {'version': '10.0.101'}}))
        with patch('shutil.which', return_value=None), patch('urllib.request.urlopen', return_value=io.BytesIO(b'# fixture')), patch.object(fetch, 'run') as run:
            self.quiet(toolchains.setup_dotnet, {})
        command = list(map(str, run.call_args_list[0].args[0]))
        self.assertIn('10.0.101', command)
        self.assertIn(str(self.root / '.tools/dotnet'), command)
        self.assertEqual(run.call_args.args[0], [toolchains.local_dotnet(), '--version'])

    def test_local_dotnet_is_discovered_by_build(self):
        executable = toolchains.local_dotnet()
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.assertEqual(toolchains.dotnet_executable({}), str(executable))
        self.assertEqual(toolchains.dotnet_executable({'dotnet_path': '/custom/dotnet'}), '/custom/dotnet')

    def test_certificate_failure_uses_verified_curl_and_explicit_ca(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'missing issuer'))
        destination = self.root / 'installer.sh'
        with patch('urllib.request.urlopen', side_effect=failure), \
             patch.object(fetch, 'HOST_OS', 'linux'), patch('shutil.which', return_value='/usr/bin/curl'), \
             patch.dict(os.environ, {'SSL_CERT_FILE': '/trusted/ca.pem'}), patch.object(fetch, 'run') as run:
            self.quiet(toolchains.download_installer, 'https://dot.net/v1/dotnet-install.sh', destination)
        command = run.call_args.args[0]
        self.assertIn('--fail', command)
        self.assertIn('--proto-redir', command)
        self.assertIn('/trusted/ca.pem', command)
        self.assertNotIn('--insecure', command)
        self.assertNotIn('-k', command)

    def test_failed_download_never_runs_installer(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'missing issuer'))
        with patch('urllib.request.urlopen', side_effect=failure), \
             patch('shutil.which', return_value='/usr/bin/curl'), \
             patch.object(fetch, 'run', side_effect=subprocess.CalledProcessError(60, ['curl'])) as run:
            with self.assertRaisesRegex(RuntimeError, '未执行安装脚本'):
                self.quiet(toolchains.setup_dotnet, {})
        self.assertEqual(run.call_count, 1)

    def test_http_error_does_not_trigger_certificate_fallback(self):
        failure = urllib.error.HTTPError('https://example.invalid', 404, 'missing', {}, None)
        with patch('urllib.request.urlopen', side_effect=failure), patch.object(fetch, 'run') as run:
            with self.assertRaises(urllib.error.HTTPError):
                toolchains.download_installer('https://example.invalid', self.root / 'installer')
        run.assert_not_called()

    def test_android_existing_tools_reused_and_packages_explicit(self):
        sdk, jdk = toolchains.android_paths({})
        for path in (jdk / 'bin/java', jdk / 'bin/javac', sdk / 'cmdline-tools/latest/bin/sdkmanager'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        with patch.object(fetch, 'HOST_OS', 'linux'), patch.object(fetch, 'run') as run:
            self.quiet(toolchains.setup_android, {'android_sdk_packages': 'platforms;android-36 build-tools;36.0.0'})
        commands = [list(map(str, call.args[0])) for call in run.call_args_list]
        self.assertFalse(any('sync' in command for command in commands))
        self.assertIn('platforms;android-36', commands[-1])
        self.assertNotIn('--licenses', commands[-1])
        self.assertEqual(os.environ['JAVA_HOME'], str(jdk.resolve()))

    def test_android_missing_tools_sync_pinned_checkout(self):
        (self.root / 'src').mkdir()
        (self.root / 'src/DEPS').touch()
        sdk, jdk = toolchains.android_paths({})
        def fake_run(command, *args, **kwargs):
            if command[:2] == ['git', 'rev-parse']:
                return 'abc123'
            if 'runhooks' in command:
                for path in (jdk / 'bin/java', jdk / 'bin/javac', sdk / 'cmdline-tools/bin/sdkmanager'):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
            return ''
        with patch.object(fetch, 'HOST_OS', 'linux'), patch.object(fetch, 'run', side_effect=fake_run) as run, patch.object(fetch, 'write_gclient') as config:
            self.quiet(toolchains.setup_android, {})
        config.assert_called_once()
        self.assertTrue(any('src@abc123' in call.args[0] for call in run.call_args_list))
