"""Round-trip backup tests using disposable Git repositories, never src itself."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backup


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backup test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / 'src'
        self.src.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Backup Test')
        self.git('config', 'user.email', 'backup@example.invalid')
        for name in ('committed.txt', 'staged.txt', 'unstaged.txt', 'deleted.txt', 'renamed.txt'):
            (self.src / name).write_text('base\n')
        (self.src / 'binary.dat').write_bytes(b'\0base\xff')
        (self.src / '.gitignore').write_text('ignored/\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.git('tag', '1.2.3.4')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()
        self.output = self.root / 'patches'

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.src), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def run_backup(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return backup.main(['--src', str(self.src), '--ver', '1.2.3.4', '--patches', str(self.output), *args])

    def result(self):
        directory = next(self.output.iterdir())
        return directory, json.loads((directory / 'manifest.json').read_text())

    def source_state(self):
        objects = self.src / '.git/objects'
        return ((self.src / '.git/index').read_bytes(),
                {str(p.relative_to(objects)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in objects.rglob('*') if p.is_file()},
                self.git('status', '--porcelain=v1', '-z'), self.git('rev-parse', 'HEAD'))

    def test_round_trip_preserves_final_content_and_source_index_objects(self):
        (self.src / 'committed.txt').write_text('committed change\n')
        self.git('commit', '-qam', 'change after tag')
        (self.src / 'staged.txt').write_text('staged change\n')
        (self.src / 'staged new.txt').write_text('staged addition\n')
        self.git('add', '.')
        (self.src / 'unstaged.txt').write_text('working change\n')
        (self.src / 'binary.dat').write_bytes(b'\0changed\xff\x01')
        (self.src / 'new binary.dat').write_bytes(b'\0new\xff')
        (self.src / 'deleted.txt').unlink()
        (self.src / 'renamed.txt').rename(self.src / '新 name.txt')
        (self.src / 'ignored').mkdir()
        (self.src / 'ignored/secret.txt').write_text('ignored')
        (self.src / 'arupa_build').mkdir()
        (self.src / 'arupa_build/BUILD.gn').write_text('generated')
        if os.name != 'nt':
            (self.src / 'link').symlink_to('unstaged.txt')
            (self.src / 'unstaged.txt').chmod(0o755)
        before = self.source_state()
        self.run_backup()
        self.assertEqual(before, self.source_state())
        directory, manifest = self.result()
        data = (directory / 'chromium.patch').read_bytes()
        self.assertEqual(manifest['patch_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest['base_commit'], self.base)
        restored = self.root / 'restored'
        self.git('clone', '-q', str(self.src), str(restored), cwd=self.root)
        self.git('checkout', '-q', '--detach', self.base, cwd=restored)
        self.git('apply', '--check', str(directory / 'chromium.patch'), cwd=restored)
        self.git('apply', '--binary', str(directory / 'chromium.patch'), cwd=restored)
        for item in manifest['changes']:
            name = item['path']
            source, result = self.src / name, restored / name
            if item['status'] == 'D':
                self.assertFalse(result.exists())
            elif source.is_symlink():
                self.assertEqual(os.readlink(source), os.readlink(result))
            else:
                self.assertEqual(source.read_bytes(), result.read_bytes(), name)
        self.assertFalse((restored / 'ignored/secret.txt').exists())
        self.assertFalse((restored / 'arupa_build').exists())
        if os.name != 'nt':
            self.assertTrue((restored / 'unstaged.txt').stat().st_mode & 0o111)

    def test_tracked_only_keeps_staged_additions(self):
        (self.src / 'new.txt').write_text('untracked')
        (self.src / 'added.txt').write_text('staged')
        self.git('add', 'added.txt')
        self.run_backup('--tracked-only')
        _, manifest = self.result()
        self.assertEqual(manifest['changes'], [{'status': 'A', 'path': 'added.txt'}])

    def test_dry_run_and_missing_baseline_do_not_write(self):
        before = self.source_state()
        self.run_backup('--dry-run')
        self.assertFalse(self.output.exists())
        with self.assertRaises(RuntimeError):
            self.run_backup('--ver', '9.9.9.9')
        self.assertFalse(self.output.exists())
        self.assertEqual(before, self.source_state())

    def test_output_inside_source_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.run_backup('--patches', str(self.src / 'patches'))
        self.assertFalse((self.src / 'patches').exists())

    def test_empty_and_repeated_backups_are_separate(self):
        self.run_backup()
        directory, manifest = self.result()
        self.assertEqual(manifest['changes'], [])
        self.assertEqual((directory / 'chromium.patch').read_bytes(), b'')
        self.run_backup()
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_nested_repository_is_not_flattened(self):
        nested = self.src / 'dependency'
        nested.mkdir()
        self.git('init', '-q', cwd=nested)
        self.git('config', 'user.name', 'Test', cwd=nested)
        self.git('config', 'user.email', 'test@example.invalid', cwd=nested)
        (nested / 'file').write_text('dependency source')
        self.git('add', '.', cwd=nested)
        self.git('commit', '-qm', 'dep', cwd=nested)
        self.git('add', 'dependency')
        self.git('commit', '-qm', 'add gitlink')
        (nested / 'file').write_text('dependency local edits')
        self.run_backup()
        _, manifest = self.result()
        self.assertEqual(manifest['changes'], [])
        self.assertIn('dependency', manifest['skipped_repository_directories'])

    def test_backup_names_increment_per_os_and_version(self):
        self.run_backup('--os', 'win')
        self.run_backup('--os', 'win')
        self.run_backup('--os', 'android')
        self.git('tag', '2.3.4.5')
        self.run_backup('--os', 'win', '--ver', '2.3.4.5')
        self.assertEqual({p.name for p in self.output.iterdir()}, {
            'src-win-1.2.3.4-1.patches', 'src-win-1.2.3.4-2.patches',
            'src-android-1.2.3.4-1.patches', 'src-win-2.3.4.5-1.patches'})
        manifest = json.loads((self.output / 'src-win-1.2.3.4-2.patches/manifest.json').read_text())
        self.assertEqual((manifest['os'], manifest['number']), ('win', 2))

    def test_explicit_number_never_overwrites_and_locked_number_is_skipped(self):
        self.run_backup('--os', 'mac', '--num', '7')
        with self.assertRaises(RuntimeError):
            self.run_backup('--os', 'mac', '--num', '7')
        lock = self.output / 'src-mac-1.2.3.4-8.patches.lock'
        lock.touch()
        self.run_backup('--os', 'mac')
        self.assertTrue((self.output / 'src-mac-1.2.3.4-9.patches').is_dir())
        self.assertTrue(lock.exists())
        with self.assertRaises(RuntimeError):
            self.run_backup('--os', 'mac', '--num', '8')
        with self.assertRaises(SystemExit):
            self.run_backup('--num', '0')

    def test_invalid_destination_and_locked_number_fail_before_scanning(self):
        self.output.write_text('not a directory')
        with patch.object(backup, 'collect_patch', side_effect=AssertionError('expensive scan')):
            with self.assertRaisesRegex(RuntimeError, '不是目录'):
                self.run_backup()
        self.output.unlink()
        self.output.mkdir()
        (self.output / 'src-win-1.2.3.4-1.patches.lock').touch()
        with patch.object(backup, 'collect_patch', side_effect=AssertionError('expensive scan')):
            with self.assertRaisesRegex(RuntimeError, '正在使用'):
                self.run_backup('--os', 'win', '--num', '1')

    def test_write_failure_removes_temporary_backup_and_releases_number(self):
        with patch.object(Path, 'write_bytes', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_backup('--os', 'win', '--num', '1')
        self.assertEqual(list(self.output.iterdir()), [])
        self.run_backup('--os', 'win', '--num', '1')
        self.assertTrue((self.output / 'src-win-1.2.3.4-1.patches').is_dir())

    def test_unchanged_files_are_not_rehashed_through_clean_filters(self):
        marker = self.root / 'filter calls'
        helper = self.root / 'identity filter.py'
        helper.write_text(
            'import sys\nfrom pathlib import Path\n'
            f'with Path({str(marker)!r}).open("ab") as stream: stream.write(b"called\\n")\n'
            'sys.stdout.buffer.write(sys.stdin.buffer.read())\n')
        command = shlex.join([Path(sys.executable).as_posix(), helper.as_posix()])
        self.git('config', 'filter.backup-probe.clean', command)
        self.git('config', 'filter.backup-probe.required', 'true')
        (self.src / '.git/info/attributes').write_text('cached.dat filter=backup-probe\n')
        (self.src / 'cached.dat').write_bytes(b'already stored content\n' * 4096)
        # Keep the fixture out of Git's same-timestamp (racy index) safety path.
        os.utime(self.src / 'cached.dat', (946684800, 946684800))
        self.git('add', 'cached.dat')
        self.git('commit', '-qm', 'cached large file')
        self.git('tag', '-f', '1.2.3.4')
        marker.write_bytes(b'')
        (self.src / 'unstaged.txt').write_text('one local edit\n')
        self.run_backup()
        self.assertEqual(marker.read_bytes(), b'', 'unchanged content was read and filtered again')
        _, manifest = self.result()
        self.assertEqual({item['path'] for item in manifest['changes']},
                         {'unstaged.txt'})

    def test_final_worktree_wins_over_staged_content(self):
        (self.src / 'staged.txt').write_text('staged intermediate\n')
        self.git('add', 'staged.txt')
        (self.src / 'staged.txt').write_text('base\n')
        self.git('rm', 'deleted.txt')
        (self.src / 'deleted.txt').write_text('recreated after staged deletion\n')
        before = self.source_state()
        self.run_backup('--tracked-only')
        self.assertEqual(before, self.source_state())
        directory, manifest = self.result()
        self.assertEqual(manifest['changes'], [{'status': 'M', 'path': 'deleted.txt'}])
        self.assertIn(b'+recreated after staged deletion', (directory / 'chromium.patch').read_bytes())

    def test_staged_ignored_addition_is_kept(self):
        (self.src / 'ignored').mkdir()
        (self.src / 'ignored/added.txt').write_text('initial staged content\n')
        self.git('add', '-f', 'ignored/added.txt')
        (self.src / 'ignored/added.txt').write_text('final working content\n')
        (self.src / 'ignored/untracked.txt').write_text('must stay excluded\n')
        self.run_backup('--tracked-only')
        directory, manifest = self.result()
        self.assertEqual(manifest['changes'], [{'status': 'A', 'path': 'ignored/added.txt'}])
        self.assertIn(b'+final working content', (directory / 'chromium.patch').read_bytes())

    def test_index_flags_do_not_hide_local_content_from_backup(self):
        self.git('update-index', '--assume-unchanged', 'staged.txt')
        self.git('update-index', '--skip-worktree', 'unstaged.txt', 'deleted.txt')
        (self.src / 'staged.txt').write_text('hidden by assume-unchanged\n')
        (self.src / 'unstaged.txt').write_text('hidden by skip-worktree\n')
        (self.src / 'deleted.txt').unlink()  # An absent sparse file is not a deletion.
        self.assertEqual(self.git('status', '--porcelain'), b'')
        before = self.source_state()
        self.run_backup()
        self.assertEqual(before, self.source_state())
        directory, manifest = self.result()
        self.assertEqual(manifest['changes'], [
            {'status': 'M', 'path': 'staged.txt'},
            {'status': 'M', 'path': 'unstaged.txt'}])
        self.assertIn(b'+hidden by assume-unchanged', (directory / 'chromium.patch').read_bytes())
        self.assertIn(b'+hidden by skip-worktree', (directory / 'chromium.patch').read_bytes())

    def test_file_replaced_by_directory_does_not_bypass_tracked_only(self):
        (self.src / 'deleted.txt').unlink()
        (self.src / 'deleted.txt').mkdir()
        (self.src / 'deleted.txt/new.txt').write_text('untracked child\n')
        self.run_backup('--tracked-only')
        _, manifest = self.result()
        self.assertEqual(manifest['changes'], [{'status': 'D', 'path': 'deleted.txt'}])

    def test_backup_reports_progress_before_completion(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            backup.main(['--src', str(self.src), '--ver', '1.2.3.4',
                         '--patches', str(self.output)])
        self.assertIn('1/4 扫描本地改动', output.getvalue())
        self.assertIn('4/4 写入备份', output.getvalue())
        self.assertIn('耗时', output.getvalue())


if __name__ == '__main__':
    unittest.main()
