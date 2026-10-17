"""内核 OOM 护栏校验器的单测（静态扫描 + 判定逻辑）。

实测部分（`run` 子命令真起宿主）没法在单测里跑：要交付件、要 X/DISPLAY、要 15s。这里锁住
两件容易写错的事：分块扫描会不会漏掉跨块的标志、以及判定表会不会放过「宿主被当子进程」的形态。
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kernel_oom_guard as guard


def write(path, data):
    with open(path, 'wb') as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())


class MarkerScanTest(unittest.TestCase):
    def test_marker_found_across_chunk_boundary(self):
        """跨块也要数得到：否则换个文件大小就漏判（.so 有 360MB，肯定跨块）。"""
        with tempfile.TemporaryDirectory() as tmp:
            original = guard.CHUNK
            guard.CHUNK = 64
            self.addCleanup(setattr, guard, 'CHUNK', original)
            marker = b'no usable sandbox'
            path = os.path.join(tmp, 'blob')
            write(path, b'x' * (64 - 5) + marker + b'y' * 10)
            self.assertEqual(guard._count_marker(path, marker), 1)

    def test_marker_counted_multiple_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'blob')
            write(path, b'handing off to' * 3)
            self.assertEqual(guard._count_marker(path, b'handing off to'), 3)

    def test_missing_file_is_zero_not_error(self):
        self.assertEqual(guard._count_marker('/nonexistent/whatever', b'x'), 0)

    def test_host_dll_markers_are_utf16(self):
        """.NET 字面量是 UTF-16LE：按 ASCII 搜会得到 0，误判成护栏缺失。"""
        with tempfile.TemporaryDirectory() as tmp:
            ascii_path = os.path.join(tmp, 'ascii.dll')
            utf16_path = os.path.join(tmp, 'utf16.dll')
            write(ascii_path, 'handing off to')
            write(utf16_path, guard._utf16le('handing off to'))
            self.assertEqual(guard._count_marker(ascii_path, b'handing off to'), 1)
            self.assertEqual(guard.scan_host_dll(ascii_path)[0][2], 0)
            self.assertEqual(guard.scan_host_dll(utf16_path)[0][2], 1)


class VerifyPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payload = self.tmp.name
        write(os.path.join(self.payload, 'libarupa_kernel.so'),
              b'...cannot point FILE_EXE at...no usable sandbox...')
        write(os.path.join(self.payload, 'NomadBrowser.Linux.KernelHost.dll'),
              guard._utf16le('handing off to') + guard._utf16le('refusing to run the full'))
        write(os.path.join(self.payload, 'arupa_render'), b'#!/bin/sh\n')

    def quiet(self, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return guard.verify_payload(*args, **kwargs)

    def test_guarded_payload_passes(self):
        self.assertTrue(self.quiet(self.payload))

    def test_missing_shell_is_a_failure(self):
        """护栏在、但薄壳没随包发出：子进程一个都起不来，仍算不合格。"""
        os.unlink(os.path.join(self.payload, 'arupa_render'))
        self.assertFalse(self.quiet(self.payload))

    def test_missing_kernel_marker_is_a_failure(self):
        write(os.path.join(self.payload, 'libarupa_kernel.so'), b'plain kernel')
        self.assertFalse(self.quiet(self.payload))

    def test_missing_host_marker_is_a_failure(self):
        write(os.path.join(self.payload, 'NomadBrowser.Linux.KernelHost.dll'),
              guard._utf16le('something else entirely'))
        self.assertFalse(self.quiet(self.payload))

    def test_find_payload_walks_into_app_layout(self):
        """mac 布局是 *.app/Contents/...：按内核库定位而不是硬编码路径。"""
        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, 'Nomad.app', 'Contents', 'Resources')
            os.makedirs(nested)
            write(os.path.join(nested, 'libarupa_kernel.so'), b'x')
            self.assertEqual(guard.find_payload(root), nested)


class VerdictTest(unittest.TestCase):
    """判定表：这些形态必须判 FAIL，否则校验器本身就是个摆设。"""

    def call(self, **overrides):
        kwargs = {'host_exe': 'NomadBrowser.Linux.KernelHost', 'shell_exe': 'arupa_render',
                  'host_peak': 1, 'child_exes': {'arupa_render'},
                  'child_types': {'--type=zygote', '--type=renderer'},
                  'peak_procs': 7, 'peak_rss': 0.61, 'exit_code': 0,
                  'log_text': '[kernelhost] shutting down'}
        kwargs.update(overrides)
        return guard.verdict(**kwargs)

    def test_guarded_shape_passes(self):
        ok, problems = self.call()
        self.assertTrue(ok, problems)

    def test_second_host_fails(self):
        """宿主被当子进程拉起来的形态：KernelHost 实例 >1。"""
        ok, problems = self.call(host_peak=3)
        self.assertFalse(ok)
        self.assertTrue(any('KernelHost 实例峰值' in problem for problem in problems))

    def test_non_shell_child_fails(self):
        ok, problems = self.call(child_exes={'arupa_render', 'NomadBrowser.Linux.KernelHost'})
        self.assertFalse(ok)
        self.assertTrue(any('不是薄壳' in problem for problem in problems))

    def test_no_zygote_observed_fails(self):
        """没看到 zygote 就说明护栏路径没被走到，结论不成立。"""
        ok, problems = self.call(child_types={'--type=renderer'})
        self.assertFalse(ok)
        self.assertTrue(any('--type=zygote' in problem for problem in problems))

    def test_caps_and_crash_and_fatal_fail(self):
        for overrides, needle in (
                ({'peak_procs': guard.CAP_PROCS + 1}, '进程数峰值'),
                ({'peak_rss': guard.CAP_RSS_GB + 1.0}, '总 RSS 峰值'),
                ({'exit_code': -6}, '退出码'),
                ({'log_text': 'FATAL ... No usable sandbox!'}, 'FATAL')):
            ok, problems = self.call(**overrides)
            self.assertFalse(ok, overrides)
            self.assertTrue(any(needle in problem for problem in problems),
                            f'{needle} 未命中: {problems}')


if __name__ == '__main__':
    unittest.main()
