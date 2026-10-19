"""Linux dlopen libraries must not use V8's executable-only TLS model."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from linux_shared_library import prepare_v8_tls
import build


class LinuxSharedLibraryTest(unittest.TestCase):
    def test_idempotent_patch_preserves_other_targets_and_dry_run(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            path = source / "v8/BUILD.gn"
            path.parent.mkdir()
            original = ('declare_args() {\n  v8_monolithic_for_shared_library = false\n}\n'
                        'config("features") {\n'
                        '  if (v8_monolithic && v8_monolithic_for_shared_library) {\n'
                        '    defines += [ "V8_TLS_USED_IN_LIBRARY" ]\n  }\n}\n')
            path.write_text(original)
            self.assertTrue(prepare_v8_tls(source, dry_run=True))
            self.assertEqual(original, path.read_text())
            self.assertTrue(prepare_v8_tls(source))
            self.assertIn("v8_tls_used_in_library = false", path.read_text())
            self.assertFalse(prepare_v8_tls(source))

    def test_changed_upstream_structure_fails_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            path = source / "v8/BUILD.gn"
            path.parent.mkdir()
            original = "  v8_monolithic_for_shared_library = false\n"
            path.write_text(original)
            with self.assertRaises(ValueError):
                prepare_v8_tls(source)
            self.assertEqual(original, path.read_text())

    def test_linux_args_enable_both_shared_library_tls_models(self):
        root = Path(__file__).resolve().parents[1]
        args = build.render_args(root / "build/linux/args.gn", "linux", "x64", "static")
        self.assertIn("v8_tls_used_in_library = true", args)
        self.assertIn("blink_heap_inside_shared_library = true", args)
        self.assertIn("use_partition_alloc_as_malloc = false", args)
        self.assertIn("enable_backup_ref_ptr_support = false", args)

    def test_allocator_workaround_does_not_change_mac_or_windows(self):
        root = Path(__file__).resolve().parents[1]
        for platform in ("mac", "win"):
            with self.subTest(platform=platform):
                args = build.render_args(root / f"build/{platform}/args.gn", platform, "x64", "static")
                self.assertNotIn("use_partition_alloc_as_malloc = false", args)
                self.assertNotIn("enable_backup_ref_ptr_support = false", args)

    def test_linux_allocator_workaround_overrides_custom_template(self):
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "args.gn"
            template.write_text('target_os = "linux"\nuse_partition_alloc_as_malloc = true\n'
                                'enable_backup_ref_ptr_support = true\n')
            args = build.render_args(template, "linux", "x64", "static")
            self.assertEqual(args.count("use_partition_alloc_as_malloc = false"), 1)
            self.assertEqual(args.count("enable_backup_ref_ptr_support = false"), 1)
            self.assertNotIn("use_partition_alloc_as_malloc = true", args)
            self.assertNotIn("enable_backup_ref_ptr_support = true", args)
