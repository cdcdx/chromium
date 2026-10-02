"""Resource boundaries and CLI wiring for automatic Ninja parallelism."""
import contextlib
import io
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import concurrency as C
import build
import fetch


class ConcurrencyTest(unittest.TestCase):
    def test_cpu_and_memory_limits(self):
        for cpus, gib, expected in ((16, 16, 6), (16, 32, 12), (4, 64, 4),
                                    (64, 4, 1), (64, 1, 1), (64, 0, 1), (1, 128, 1)):
            with self.subTest(cpus=cpus, gib=gib):
                self.assertEqual(C.jobs_for_resources(cpus, gib * C.GIB), expected)
        self.assertEqual(C.jobs_for_resources(64, None), 1)

    def test_cpu_affinity_and_unknown_cpu(self):
        with patch.object(C.os, 'cpu_count', return_value=32), patch.object(C.os, 'sched_getaffinity', return_value={1, 3}, create=True):
            self.assertEqual(C.cpu_capacity(), 2)
        with patch.object(C.os, 'cpu_count', return_value=None), patch.object(C.os, 'sched_getaffinity', side_effect=OSError, create=True):
            self.assertEqual(C.cpu_capacity(), 1)

    def test_memory_query_failure_is_conservative(self):
        with patch.object(C.platform, 'system', return_value='Linux'), patch.object(C.os, 'sysconf', side_effect=OSError):
            self.assertIsNone(C.physical_memory())
            self.assertEqual(C.automatic_jobs()[0], 1)

    def test_ninja_receives_automatic_or_explicit_jobs(self):
        # Check orchestration without touching real sources or running tools.
        with patch.object(fetch, 'DRY_RUN', True), patch.object(fetch, 'chromium_version', return_value='1.2.3.4'), \
             patch.object(build, 'prepare_project'), patch.object(build, 'render_args', return_value=''), \
             patch.object(build, 'args_template', return_value=Path('args.gn')), \
             patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'exists', return_value=False), \
             patch.object(fetch, 'tool_environment'), patch.object(fetch, 'run') as run, \
             patch.object(build, 'automatic_jobs', return_value=(6, 'fixture')) as automatic, \
             contextlib.redirect_stdout(io.StringIO()):
            build.main(['desktop', 'build', '--os', 'linux', '--arch', 'x64', '--dry-run'])
            command = next(call.args[0] for call in run.call_args_list if '-C' in call.args[0])
            self.assertEqual(command[command.index('-j') + 1], '6')
            automatic.assert_called_once()
            automatic.reset_mock()
            run.reset_mock()
            build.main(['desktop', 'build', '--os', 'linux', '--arch', 'x64', '--jobs', '3', '--dry-run'])
            command = next(call.args[0] for call in run.call_args_list if '-C' in call.args[0])
            self.assertEqual(command[command.index('-j') + 1], '3')
            automatic.assert_not_called()
