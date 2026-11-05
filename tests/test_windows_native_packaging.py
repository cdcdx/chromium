"""Run the Windows packager against isolated native-host delivery fixtures."""
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGER = ROOT / 'scripts/packaging/package-arupa_desktop.ps1'
FIXTURES = ROOT / 'arupa_desktop/tools/tests/test_transfer_delivery.py'
spec = importlib.util.spec_from_file_location('native_delivery_fixtures', FIXTURES)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@unittest.skipUnless(platform.system() == 'Windows', 'Windows packager')
class WindowsNativePackagingTests(unittest.TestCase):
    def package(self, host_bytes, check):
        with tempfile.TemporaryDirectory(prefix='arupa-native-package-') as temporary:
            root = Path(temporary).resolve()
            out = root / 'out'
            out.mkdir()
            (out / 'arupa_kernel.dll').write_bytes(fixtures.pe_fixture(sorted(fixtures.gate.REQUIRED)))
            if host_bytes is not None:
                (out / 'arupa_desktop.exe').write_bytes(host_bytes)
            for name in ('arupa_render.exe', 'arupa_plugin_host.exe', 'content_shell.pak',
                         'devtools_resources.pak', 'icudtl.dat', 'snapshot_blob.bin',
                         'gen/extensions/strings/extensions_strings_en-US.pak',
                         'gen/extensions/extensions_renderer_generated_resources.pak'):
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            (out / 'arupa-build.json').write_text('{"schema":2}')
            headers = root / 'headers'
            headers.mkdir()
            (headers / 'arupa_kernel_capi.h').write_text('// fixture')
            dist = root / 'dist'
            result = subprocess.run(
                [shutil.which('pwsh') or shutil.which('powershell.exe') or 'powershell.exe', '-NoProfile',
                 '-ExecutionPolicy', 'Bypass', '-File', str(PACKAGER), '--arch', 'x64',
                 '--ver', '0.0.0.0', '--out', str(out), '--dist-dir', str(dist),
                 '--num', '1', '--no-package', '--include-dir', str(headers)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            delivery = dist / 'arupa-win-x64-0.0.0.0-static-1'
            check(result, delivery)

    def test_production_delivery_keeps_native_host_and_compatibility_dll(self):
        host = fixtures.pe_fixture(sorted(fixtures.gate.NATIVE_REQUIRED))
        def check(result, delivery):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((delivery / 'kernel/arupa_desktop.exe').read_bytes(), host)
            self.assertTrue((delivery / 'kernel/arupa_kernel.dll').is_file())
            report = json.loads((delivery / 'transfer-abi.json').read_text(encoding='utf-8'))
            self.assertEqual(report['nativeHost']['staticAbi'], 'passed')
            self.assertIn('kernel/arupa_desktop.exe', (delivery / 'SHA256SUMS.txt').read_text(encoding='utf-8'))
        self.package(host, check)

    def test_missing_or_invalid_native_host_cannot_leave_a_delivery(self):
        invalid_exports = fixtures.pe_fixture(['arupa_free'])
        wrong_arch = fixtures.pe_fixture(sorted(fixtures.gate.NATIVE_REQUIRED))
        struct.pack_into('<H', wrong_arch, 0x84, 0xAA64)
        for host in (None, invalid_exports, wrong_arch):
            with self.subTest(kind='missing' if host is None else 'invalid'):
                def check(result, delivery):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(delivery.exists(), 'failed package must be removed')
                    self.assertIn('arupa_desktop.exe', result.stdout + result.stderr)
                self.package(host, check)


if __name__ == '__main__':
    unittest.main()
