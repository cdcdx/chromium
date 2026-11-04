"""Compatibility launcher checks; publisher tests live in mivpn-plugin/tools."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'scripts/sign_mivpn_plugin.py'


class MivpnSignerLauncherTests(unittest.TestCase):
    def launch(self, project):
        environment = dict(os.environ, MIVPN_PLUGIN_ROOT=str(project))
        return subprocess.run([sys.executable, str(ENTRY), '--help'],
                              capture_output=True, text=True, env=environment)

    def test_missing_project_reports_configuration(self):
        with tempfile.TemporaryDirectory(prefix='mivpn-launcher-') as directory:
            result = self.launch(Path(directory) / 'absent')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MIVPN_PLUGIN_ROOT', result.stderr)

    def test_explicit_external_project_is_used(self):
        with tempfile.TemporaryDirectory(prefix='mivpn-launcher-') as directory:
            project = Path(directory)
            (project / 'tools').mkdir()
            (project / 'tools/sign_mivpn_plugin.py').write_text(
                "print('external publisher tool')\n")
            result = self.launch(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'external publisher tool')

    @unittest.skipUnless((ROOT / 'mivpn-plugin/tools/sign_mivpn_plugin.py').is_file(),
                         'independent publisher project is not present')
    def test_workspace_project_help(self):
        environment = dict(os.environ)
        environment.pop('MIVPN_PLUGIN_ROOT', None)
        result = subprocess.run([sys.executable, str(ENTRY), '--help'],
                                capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('keygen', result.stdout)


if __name__ == '__main__':
    unittest.main()
