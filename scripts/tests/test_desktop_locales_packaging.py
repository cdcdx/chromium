"""Exercise the actual POSIX packager with small, isolated delivery fixtures."""
import platform
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PACKAGER = ROOT / "scripts/packaging/package-arupa_desktop.sh"


@unittest.skipUnless(platform.system() in ("Darwin", "Linux"), "POSIX packager")
class LocalePackagingTest(unittest.TestCase):
    def package(self, target_os, locale_files, check, symbols=None):
        with tempfile.TemporaryDirectory(prefix="arupa-package-test-") as tmp:
            root = Path(tmp)
            # These tests isolate resource packaging, not native ABI behavior.
            # The ABI checker has separate PE fixtures and real dylib acceptance.
            tools = root / 'tools'; tools.mkdir()
            nm = tools / 'llvm-nm'
            names = symbols if symbols is not None else ['arupa_kernel_transfer_runtime', 'arupa_kernel_set_ext_api_handler_ctx', 'arupa_kernel_respond_ext_api', 'arupa_free']
            output = '\n'.join('_'+n if target_os == 'mac' else n+' T 0 1' for n in names)
            nm.write_text('#!/usr/bin/env python3\nprint('+repr(output)+')\n'); nm.chmod(0o755)
            env = dict(os.environ, PATH=str(tools)+os.pathsep+os.environ['PATH'])
            out = root / "out"
            out.mkdir()
            files = ["arupa_render", "arupa_plugin_host", "content_shell.pak",
                     "devtools_resources.pak", "icudtl.dat", "snapshot_blob.bin",
                     "vk_swiftshader_icd.json", "hyphen-data/manifest.json",
                     "hyphen-data/en-us.hyb",
                     "gen/extensions/strings/extensions_strings_en-US.pak",
                     "gen/extensions/extensions_renderer_generated_resources.pak"]
            if target_os == "mac":
                files += ["libEGL.dylib", "libGLESv2.dylib", "libvk_swiftshader.dylib",
                          "libvulkan.dylib", "libVkICD_mock_icd.dylib",
                          "libVkLayer_khronos_validation.dylib",
                          "Libraries/libtest_trace_processor.dylib"]
                # A minimal Mach-O header is enough for the packager's file(1)
                # architecture check; no fixture executable is ever launched.
                (out / "libarupa_kernel.dylib").write_bytes(
                    struct.pack("<8I", 0xFEEDFACF, 0x100000C, 0, 6, 0, 0, 0, 0))
                arch = "arm64"
            else:
                files += ["libEGL.so", "libGLESv2.so", "libvk_swiftshader.so",
                          "libvulkan.so.1", "libvulkan.so", "libVkICD_mock_icd.so",
                          "libVkLayer_khronos_validation.so"]
                (out / "libarupa_kernel.so").write_bytes(
                    b"\x7fELF\x02\x01\x01" + bytes(9) +
                    struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, 0, 0,
                                64, 0, 0, 0, 0, 0))
                arch = "x64"
            for name in files:
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            for name in ("angledata", "resources"):
                (out / name).mkdir()
            if locale_files is not None:
                (out / "locales").mkdir()
                for name, data in locale_files.items():
                    (out / "locales" / name).write_bytes(data)
            result = subprocess.run(
                ["bash", str(PACKAGER), "--os", target_os, "--arch", arch,
                 "--ver", "0.0.0.0", "--out", str(out), "--num", "1",
                 "--dist-dir", str(root / "dist"), "--no-package"],
                capture_output=True, text=True, timeout=30, env=env)
            delivery = root / "dist" / f"arupa-{target_os}-{arch}-0.0.0.0-static-1"
            check(result, delivery)

    def test_legacy_build_has_no_false_missing_warning(self):
        def check(result, delivery):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("可选件缺失", result.stderr)
            self.assertIn("旧构建未生成独立语言包", result.stdout)
            self.assertFalse((delivery / "kernel/locales").exists())
        for os_name in ("mac", "linux"):
            with self.subTest(os=os_name):
                self.package(os_name, None, check)

    def test_missing_transfer_runtime_rejects_and_removes_delivery(self):
        def check(result, delivery):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('arupa_kernel_transfer_runtime', result.stderr)
            self.assertFalse(delivery.exists())
        for os_name in ('mac', 'linux'):
            with self.subTest(os=os_name): self.package(os_name, None, check, symbols=['arupa_free'])

    def test_translations_and_fallback_are_delivered(self):
        packs = {"en-US.pak": b"English", "zh-CN.pak": b"Chinese",
                 "de.pak": b"stale German", "en-XA.pak": b"stale pseudo-locale"}
        def check(result, delivery):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("可选件缺失", result.stderr)
            self.assertEqual(sorted(p.name for p in (delivery / "kernel/locales").iterdir()),
                             ["en-US.pak", "zh-CN.pak"])
            for name in ("en-US.pak", "zh-CN.pak"):
                self.assertEqual((delivery / "kernel/locales" / name).read_bytes(), packs[name])
        for os_name in ("mac", "linux"):
            with self.subTest(os=os_name):
                self.package(os_name, packs, check)

    def test_missing_or_empty_supported_language_rejects_partial_delivery(self):
        def check(result, delivery):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("locales/", result.stderr)
            self.assertFalse(delivery.exists(), "failed package must be removed")
        for os_name in ("mac", "linux"):
            for packs in ({}, {"zh-CN.pak": b"Chinese"}, {"en-US.pak": b"English"},
                          {"en-US.pak": b"", "zh-CN.pak": b"Chinese"},
                          {"en-US.pak": b"English", "zh-CN.pak": b""}):
                with self.subTest(os=os_name, packs=list(packs)):
                    self.package(os_name, packs, check)


if __name__ == "__main__":
    unittest.main()
