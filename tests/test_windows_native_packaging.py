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

RUNTIME_GEN_PAKS = (
    'gen/chrome/renderer_resources_resources.pak',
    'gen/components/strings/components_strings_en-US.pak',
    'gen/extensions/strings/extensions_strings_en-US.pak',
    'gen/extensions/extensions_renderer_generated_resources.pak',
)
INTERMEDIATE_GEN_PAKS = (
    'gen/components/test resources/fixture.pak',
    'gen/components/strings/components_strings_fr.pak',
    'gen/components/strings/components_strings_en-US_MASCULINE.pak',
    'gen/extensions/extensions_browser_resources.pak',
)


@unittest.skipUnless(platform.system() == 'Windows', 'Windows packager')
class WindowsNativePackagingTests(unittest.TestCase):
    def package(self, host_bytes, check, missing_gen_pak=None, child_bytes=True):
        with tempfile.TemporaryDirectory(prefix='arupa-native-package-') as temporary:
            root = Path(temporary).resolve()
            out = root / 'out'
            out.mkdir()
            (out / 'arupa_kernel.dll').write_bytes(fixtures.pe_fixture(sorted(fixtures.gate.REQUIRED)))
            if host_bytes is not None:
                (out / 'arupa_desktop.exe').write_bytes(host_bytes)
            if child_bytes is True:
                child_bytes = fixtures.pe_fixture(sorted(fixtures.gate.BOOTSTRAP_REQUIRED))
            if child_bytes is not None:
                (out / 'arupa_render.exe').write_bytes(child_bytes)
            archive = out / 'obj/sandbox/win/sandbox.lib'
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'compiled sandbox fixture')
            for name in ('arupa_plugin_host.exe', 'content_shell.pak',
                         'devtools_resources.pak', 'icudtl.dat', 'snapshot_blob.bin',
                         'custom_root.pak'):
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            for name in RUNTIME_GEN_PAKS + INTERMEDIATE_GEN_PAKS:
                if name == missing_gen_pak:
                    continue
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode('utf-8'))
            (out / 'arupa-build.json').write_text('{"schema":2}')
            sdk = root / 'sdk'
            sdk.mkdir()
            for name in ('mivpn-browser.dll', 'mivpn-browser.h', 'mivpn-browser.lib'):
                (sdk / name).write_bytes(b'fixture')
            headers = root / 'headers'
            headers.mkdir()
            (headers / 'arupa_kernel_capi.h').write_text('// fixture')
            dist = root / 'dist'
            result = subprocess.run(
                [shutil.which('pwsh') or shutil.which('powershell.exe') or 'powershell.exe', '-NoProfile',
                 '-ExecutionPolicy', 'Bypass', '-File', str(PACKAGER), '--arch', 'x64',
                 '--ver', '0.0.0.0', '--out', str(out), '--dist-dir', str(dist),
                 '--num', '1', '--no-package', '--include-dir', str(headers),
                 '--mivpn-sdk', str(sdk)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
            delivery = dist / 'arupa-win-x64-0.0.0.0-static-1'
            check(result, delivery)

    def test_production_delivery_excludes_stale_static_host_and_keeps_dll(self):
        host = fixtures.pe_fixture(sorted(fixtures.gate.NATIVE_REQUIRED))
        def check(result, delivery):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((delivery / 'kernel/arupa_desktop.exe').exists())
            self.assertTrue((delivery / 'kernel/arupa_kernel.dll').is_file())
            report = json.loads((delivery / 'transfer-abi.json').read_text(encoding='utf-8'))
            self.assertEqual(report['childBootstrap']['staticAbi'], 'passed')
            self.assertNotIn('kernel/arupa_desktop.exe', (delivery / 'SHA256SUMS.txt').read_text(encoding='utf-8'))
        self.package(host, check)

    def test_only_runtime_gen_paks_are_delivered(self):
        host = fixtures.pe_fixture(sorted(fixtures.gate.NATIVE_REQUIRED))
        def check(result, delivery):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            kernel = delivery / 'kernel'
            delivered = sorted(p.relative_to(kernel).as_posix()
                               for p in (kernel / 'gen').rglob('*.pak'))
            self.assertEqual(delivered, sorted(RUNTIME_GEN_PAKS))
            for relative in RUNTIME_GEN_PAKS:
                self.assertEqual((kernel / relative).read_bytes(), relative.encode('utf-8'))
            self.assertEqual((kernel / 'custom_root.pak').read_bytes(), b'fixture')
            sums = (delivery / 'SHA256SUMS.txt').read_text(encoding='utf-8')
            for relative in RUNTIME_GEN_PAKS:
                self.assertIn('kernel/' + relative, sums)
            for relative in INTERMEDIATE_GEN_PAKS:
                self.assertNotIn(relative, sums)
        self.package(host, check)

    def test_missing_required_extension_pak_rejects_partial_delivery(self):
        host = fixtures.pe_fixture(sorted(fixtures.gate.NATIVE_REQUIRED))
        for relative in RUNTIME_GEN_PAKS[2:]:
            with self.subTest(resource=relative):
                def check(result, delivery):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(Path(relative).name, result.stdout + result.stderr)
                    self.assertFalse(delivery.exists(), 'failed package must be removed')
                self.package(host, check, missing_gen_pak=relative)

    def test_missing_or_invalid_child_bootstrap_cannot_leave_a_delivery(self):
        invalid_exports = fixtures.pe_fixture(['arupa_free'])
        wrong_arch = fixtures.pe_fixture(sorted(fixtures.gate.NATIVE_REQUIRED))
        struct.pack_into('<H', wrong_arch, 0x84, 0xAA64)
        for host in (None, invalid_exports, wrong_arch):
            with self.subTest(kind='missing' if host is None else 'invalid'):
                def check(result, delivery):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(delivery.exists(), 'failed package must be removed')
                self.package(None, check, child_bytes=host)


if __name__ == '__main__':
    unittest.main()
