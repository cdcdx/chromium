"""Read-only repository inventory using real, local Git repositories."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fetch
import repositories as R


class RepositoriesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='repo inventory ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def init(self, name, commit=True):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, 'init', '-b', 'main')
        self.git(path, 'config', 'user.name', 'Test')
        self.git(path, 'config', 'user.email', 'test@example.invalid')
        if commit:
            (path / 'file').write_text('one')
            self.git(path, 'add', 'file')
            self.git(path, '-c', 'commit.gpgsign=false', 'commit', '-m', 'old-message')
            (path / 'file').write_text('two')
            self.git(path, '-c', 'commit.gpgsign=false', 'commit', '-am', 'latest-message')
        return path

    def show(self, *actions):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = R.show(self.root, actions)
        return result, output.getvalue()

    def test_latest_log_only_immediate_repos_and_worktrees_without_mutation(self):
        self.init('.')
        repo = self.init('project')
        nested = self.init('project/dependency')
        self.git(repo, 'worktree', 'add', '-b', 'work', str(self.root / 'linked'))
        if os.name != 'nt':
            (self.root / 'alias').symlink_to(repo, target_is_directory=True)
        before = (repo / '.git/index').read_bytes()
        result, output = self.show('log')
        self.assertEqual(result, 0)
        self.assertIn('共 2 个仓库', output)
        self.assertEqual(output.count('latest-message'), 2)
        self.assertNotIn('old-message', output)
        self.assertNotIn('project/dependency', output)
        self.assertNotIn('=== . ===', output)
        self.assertIn('linked', output)
        self.assertEqual((repo / '.git/index').read_bytes(), before)
        self.assertEqual(self.git(repo, 'branch', '--show-current'), 'main')

    def test_branch_reports_local_and_detached_head_but_not_remote(self):
        repo = self.init('project')
        self.git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git(repo, 'checkout', '--detach')
        result, output = self.show('branch')
        self.assertEqual(result, 0)
        self.assertNotIn('remotes/origin/main', output)
        self.assertIn('main', output)
        self.assertIn('* ', output)
        self.assertIn('HEAD', output)

    def test_empty_and_bare_repositories(self):
        self.init('empty', commit=False)
        bare = self.root / 'bare.git'
        bare.mkdir()
        self.git(bare, 'init', '--bare')
        result, output = self.show('log', 'branch')
        self.assertEqual(result, 0)
        self.assertIn('共 2 个仓库', output)
        self.assertEqual(output.count('(尚无提交)'), 2)

    def test_corrupt_repository_does_not_hide_healthy_repository(self):
        bad = self.root / 'a-broken'
        bad.mkdir()
        (bad / '.git').write_text('gitdir: /nonexistent/gitdir\n')
        self.init('z-valid')
        result, output = self.show('log')
        self.assertEqual(result, 1)
        self.assertIn('[ERROR]', output)
        self.assertIn('latest-message', output)

    def test_cli_uses_cwd_without_config_or_fetch(self):
        with patch.object(Path, 'cwd', return_value=self.root), patch.object(fetch, 'load_config', side_effect=AssertionError('config')), \
             patch.object(R, 'show', return_value=0) as show:
            self.assertEqual(fetch.main(['log', 'branch', 'log']), 0)
        show.assert_called_once_with(self.root, ['log', 'branch'], False)
        with contextlib.redirect_stderr(io.StringIO()), patch.object(R, 'show') as show:
            with self.assertRaises(SystemExit):
                fetch.main(['log', 'all'])
            show.assert_not_called()

    def test_dry_run_and_no_repositories(self):
        self.init('repo')
        with patch.object(R, 'git', side_effect=AssertionError('executed')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.show(self.root, ['log'], dry_run=True), 0)
        empty = self.root / 'no repos'
        empty.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.show(empty, ['branch']), 0)
