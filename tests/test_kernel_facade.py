"""交付门面一致性：ArupaKernel.dll 必须由它旁边的源码编出，而不是"够大就算数"。

事故背景（2026-10-08）：交付 dist/arupa-mac-arm64-...-static-4 里 .cs 已是新源码，
dotnet/ArupaKernel.dll 却是 10-04 的旧产物，还被写进 SHA256SUMS.txt；Windows 宿主按
HintPath 引用它，编译期就撞 CS0117/CS1061。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kernel_facade

BIG = kernel_facade.MIN_FACADE_BYTES + 1024


class FacadeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='arupa facade test ')
        self.addCleanup(self.temp.cleanup)
        self.sdk = Path(self.temp.name) / 'dotnet'
        self.sdk.mkdir()
        (self.sdk / 'ArupaKernel.csproj').write_text('<Project />')
        (self.sdk / 'Interop.cs').write_text('// interop')
        (self.sdk / 'ArupaBrowser.cs').write_text('// wrapper')
        self.commands = []
        self.dll = self.sdk / kernel_facade.ASSEMBLY_NAME

    def fake_build(self, payload=b'fresh'):
        """替身 dotnet build：把产物写进工程 bin/ 下（真实 SDK 的落点）。"""
        def runner(command, cwd):
            self.commands.append([str(part) for part in command])
            platform = next((str(c).split('=', 1)[1] for c in command
                             if str(c).startswith('-p:Platform=')), 'x64')
            out = self.sdk / 'bin' / platform / 'Release' / 'net10.0'
            out.mkdir(parents=True, exist_ok=True)
            (out / kernel_facade.ASSEMBLY_NAME).write_bytes(payload.ljust(BIG, b'x'))
            (out / kernel_facade.DOCS_NAME).write_text('<doc />')
            return True
        return runner

    @staticmethod
    def failing():
        return lambda command, cwd: False

    def test_digest_follows_source_content(self):
        first = kernel_facade.source_digest(self.sdk)
        self.assertEqual(first, kernel_facade.source_digest(self.sdk))
        (self.sdk / 'Interop.cs').write_text('// interop changed')
        self.assertNotEqual(first, kernel_facade.source_digest(self.sdk))
        # 产物本身不参与摘要：改 DLL 不该让"门面与源码一致"的判定失效
        changed = kernel_facade.source_digest(self.sdk)
        self.dll.write_bytes(b'noise'.ljust(BIG, b'n'))
        self.assertEqual(changed, kernel_facade.source_digest(self.sdk))

    def test_existing_but_stale_dll_is_rebuilt_and_then_reused(self):
        self.dll.write_bytes(b'old'.ljust(BIG, b'o'))
        self.assertFalse(kernel_facade.is_current(self.sdk))
        result = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.fake_build(b'fresh'))
        self.assertEqual('rebuilt', result.state)
        self.assertEqual(b'fresh'.ljust(BIG, b'x'), self.dll.read_bytes())
        self.assertTrue((self.sdk / kernel_facade.DOCS_NAME).is_file())
        self.assertTrue(kernel_facade.is_current(self.sdk))
        again = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.fake_build(b'second'))
        self.assertEqual('current', again.state)
        self.assertEqual(1, len(self.commands), '摘要一致时不该再编一次')
        self.assertEqual(b'fresh'.ljust(BIG, b'x'), self.dll.read_bytes())

    def test_sources_changed_makes_the_shipped_dll_stale_again(self):
        kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.fake_build(b'fresh'))
        self.assertTrue(kernel_facade.is_current(self.sdk))
        (self.sdk / 'ArupaBrowser.cs').write_text('// wrapper changed after packaging')
        self.assertFalse(kernel_facade.is_current(self.sdk))

    def test_build_command_never_outputs_into_the_project_dir(self):
        kernel_facade.ensure(self.sdk, 'dotnet', arch='arm64', runner=self.fake_build())
        command = self.commands[0]
        self.assertEqual(str(kernel_facade.project(self.sdk)), command[2])
        self.assertIn('-p:Platform=ARM64', command)
        self.assertNotIn('-o', command)      # 🔴 -o 到工程目录会编出 4KB 空程序集且退出码 0

    def test_extra_props_reach_the_build_command(self):
        restore = '-p:RestoreConfigFile=/tmp/private.config'
        kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', extra_props=(restore,),
                             runner=self.fake_build())
        self.assertIn(restore, self.commands[0])

    def test_failed_rebuild_drops_the_mismatching_artifact(self):
        self.dll.write_bytes(b'old'.ljust(BIG, b'o'))
        result = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.failing())
        self.assertEqual('failed', result.state)
        self.assertFalse(self.dll.exists(), '编不出来就必须删掉过期产物')
        self.assertTrue((self.sdk / 'Interop.cs').is_file(), '源码不许动')
        self.assertIn('已删除过期产物', result.text)

    def test_keep_on_failure_option_leaves_the_delivery_untouched(self):
        self.dll.write_bytes(b'old'.ljust(BIG, b'o'))
        result = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.failing(),
                                      drop_on_failure=False)
        self.assertEqual('failed', result.state)
        self.assertTrue(self.dll.is_file())

    def test_missing_dotnet_drops_the_mismatching_artifact(self):
        self.dll.write_bytes(b'old'.ljust(BIG, b'o'))
        result = kernel_facade.ensure(self.sdk, None)
        self.assertEqual('failed', result.state)
        self.assertFalse(self.dll.exists())

    def test_sources_only_delivery_is_built(self):
        result = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.fake_build())
        self.assertEqual('rebuilt', result.state)
        self.assertTrue(kernel_facade.is_current(self.sdk))

    def test_dry_run_reports_without_touching_files(self):
        self.dll.write_bytes(b'old'.ljust(BIG, b'o'))
        result = kernel_facade.ensure(self.sdk, 'dotnet', arch='arm64', runner=self.fake_build(),
                                      dry_run=True)
        self.assertEqual('dry-run', result.state)
        self.assertEqual(b'old'.ljust(BIG, b'o'), self.dll.read_bytes())
        self.assertIn('-p:Platform=ARM64', result.detail)
        self.assertEqual([], self.commands)

    def test_delivery_without_the_facade_project_is_skipped(self):
        empty = Path(self.temp.name) / 'no-facade'
        empty.mkdir()
        result = kernel_facade.ensure(empty, 'dotnet', runner=self.fake_build())
        self.assertEqual('no-project', result.state)
        self.assertEqual([], self.commands)

    def test_in_place_repair_flags_a_stale_delivery_manifest(self):
        """就地修复已出包的交付时，得提醒它的清单已经失真（出包流程不受影响）。"""
        self.dll.write_bytes(b'old'.ljust(BIG, b'o'))
        (self.sdk.parent / 'SHA256SUMS.txt').write_text('deadbeef  dotnet/ArupaKernel.dll\n')
        result = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.fake_build())
        self.assertEqual('rebuilt', result.state)
        self.assertIn('SHA256SUMS.txt', result.detail)
        (self.sdk.parent / 'SHA256SUMS.txt').unlink()
        (self.sdk / 'ArupaBrowser.cs').write_text('// changed after packaging')
        fresh = kernel_facade.ensure(self.sdk, 'dotnet', arch='x64', runner=self.fake_build())
        self.assertEqual('rebuilt', fresh.state)
        self.assertEqual('', fresh.detail)

    def test_ref_only_assembly_counts_as_missing(self):
        self.dll.write_bytes(b'ref')          # ~4KB 的 ref 程序集：只有签名没有实现
        (self.sdk / kernel_facade.DIGEST_NAME).write_text(
            kernel_facade.source_digest(self.sdk), encoding='utf-8')
        self.assertFalse(kernel_facade.is_current(self.sdk))


if __name__ == '__main__':
    unittest.main()
