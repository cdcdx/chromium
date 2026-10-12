"""内核仓的 Chromium 侧补丁必须能（幂等地）打进 src。

事故背景（2026-10-12）：批 11 要求 src 侧同时打 content/public/browser/websocket_connect_context.h
那个补丁，而构建流程从不应用它，症状是内核编译期一句 "file not found"。更阴的是 Windows 上
内核仓 core.autocrlf=true 会把 .patch 检出成 CRLF，而 Chromium 源码是 LF —— `git apply` 会
以 "patch does not apply" 拒绝全部 hunk，于是"补丁明明在、也明明没打上"同时成立。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kernel_src_patches

ORIGINAL = 'one\ntwo\nthree\n'


class PatchApplyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='arupa src patches test ')
        self.addCleanup(self.temp.cleanup)
        self.src = Path(self.temp.name) / 'src'
        self.src.mkdir()
        self.patches = Path(self.temp.name) / 'patches'
        self.patches.mkdir()
        self.file = self.src / 'a.txt'
        self.file.write_text(ORIGINAL)
        self.git('init')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'arupa test')
        self.git('add', 'a.txt')
        self.git('commit', '-m', 'base')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.src, capture_output=True, text=True,
                              encoding='utf-8', check=True).stdout

    def make_patch(self, name='change.patch', mutated='one\nTWO\nthree\n', crlf=False):
        """用 git diff 造一个真补丁（先改、取 diff、再还原），可选转成 CRLF 模拟 autocrlf。"""
        self.file.write_text(mutated)
        diff = subprocess.run(['git', 'diff'], cwd=self.src, capture_output=True).stdout
        self.file.write_text(ORIGINAL)
        data = diff.replace(b'\r\n', b'\n')
        (self.patches / name).write_bytes(data.replace(b'\n', b'\r\n') if crlf else data)

    def test_pending_patch_is_applied(self):
        self.make_patch()
        results = kernel_src_patches.ensure_applied(self.src, self.patches,
                                                    logger=lambda _message: None)
        self.assertEqual([('change.patch', 'applied-now')], results)
        self.assertEqual('one\nTWO\nthree\n', self.file.read_text())

    def test_second_run_is_idempotent(self):
        self.make_patch()
        quiet = lambda _message: None                                        # noqa: E731
        kernel_src_patches.ensure_applied(self.src, self.patches, logger=quiet)
        applied = self.file.read_text()
        results = kernel_src_patches.ensure_applied(self.src, self.patches, logger=quiet)
        self.assertEqual([('change.patch', 'applied')], results)
        self.assertEqual(applied, self.file.read_text())

    def test_crlf_patch_still_applies(self):
        """autocrlf 把 .patch 变成 CRLF 之后，git apply 对 LF 源码树会全盘拒绝 —— 必须归一化。"""
        self.make_patch(crlf=True)
        self.assertIn(b'\r\n', (self.patches / 'change.patch').read_bytes())
        kernel_src_patches.ensure_applied(self.src, self.patches, logger=lambda _message: None)
        self.assertEqual('one\nTWO\nthree\n', self.file.read_text())

    def test_conflicting_patch_names_the_patch_and_the_tree(self):
        self.make_patch('conflict.patch', mutated='one\nTWO\nthree\n')
        # 把补丁的上下文改掉：变成对着另一份基线写的
        patch = self.patches / 'conflict.patch'
        patch.write_bytes(patch.read_bytes().replace(b'three', b'THREE'))
        with self.assertRaises(kernel_src_patches.PatchError) as caught:
            kernel_src_patches.ensure_applied(self.src, self.patches, logger=lambda _message: None)
        message = str(caught.exception)
        self.assertIn('conflict.patch', message)
        self.assertIn(str(self.src), message)

    def test_missing_patches_directory_is_a_no_op(self):
        self.assertEqual([], kernel_src_patches.ensure_applied(self.src, self.patches / 'nope'))

    def test_dry_run_reports_without_touching_the_tree(self):
        self.make_patch()
        results = kernel_src_patches.ensure_applied(self.src, self.patches, dry_run=True,
                                                    logger=lambda _message: None)
        self.assertEqual([('change.patch', 'pending')], results)
        self.assertEqual(ORIGINAL, self.file.read_text())

    def test_normalize_handles_crlf_and_lone_cr(self):
        self.assertEqual('a\nb\n', kernel_src_patches.normalize('a\r\nb\r'))


if __name__ == '__main__':
    unittest.main()
