"""User-facing ZIP signing workflow, using only synthetic resources/temp keys."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import sign_mivpn_plugin as tool


class SignMivpnPluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = tool.package.openssl_binary()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mivpn-friendly-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.key, self.public = self.root / 'release.pem', self.root / 'release.pub.pem'
        tool.package.openssl_run(self.binary, ['genpkey', '-algorithm', 'ED25519', '-out', self.key])
        tool.package.openssl_run(self.binary, ['pkey', '-in', self.key, '-pubout', '-out', self.public])
        # Only copy public Chromium identity metadata, never real account nodes.
        manifest = json.loads((tool.BACKEND / 'product/manifest.json').read_text())
        manifest['version'] = '0.0.13'
        self.resources = {
            'manifest.json': json.dumps(manifest).encode(),
            'background.js': b'// RESOURCE_PRIVATE_SENTINEL_MUST_NOT_BE_LOGGED\n',
            'popup.html': b'<html>synthetic</html>',
            'assets/popup.css': b'body{}',
            'assets/popup.js': b'// synthetic UI',
            'offline-nodes.json': b'[{"view":{"id":"synthetic","region":"JP"},"node":{"id":"synthetic"}}]',
        }
        self.input = self.root / 'mivpn-extension-dev_plugin-ref-parity-202611021752.zip'
        self.input.write_bytes(tool.package.zip_bytes(self.resources))
        self.original = self.input.read_bytes()

    def run_cli(self, *arguments, ok=True, env=None):
        environment = os.environ.copy()
        environment.pop('MIVPN_SIGNING_KEY_FILE', None)
        if env:
            environment.update(env)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/sign_mivpn_plugin.py'),
                                 *map(str, arguments), '--openssl', self.binary],
                                capture_output=True, env=environment)
        combined = result.stdout + result.stderr
        self.assertFalse(b'PRIVATE KEY-----' in combined, 'Private PEM must never be logged')
        self.assertFalse(b'RESOURCE_PRIVATE_SENTINEL' in combined, 'Resource bytes must never be logged')
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def sign(self, **kwargs):
        return self.run_cli('sign', '--input', self.input, '--key', self.key, **kwargs)

    def test_user_named_zip_roundtrip_preserves_all_business_bytes(self):
        signed = self.sign()
        output = Path(signed['output'])
        self.assertEqual(output, self.input.with_name(self.input.stem + '-signed.zip'))
        self.assertEqual(self.original, self.input.read_bytes())
        resources = tool.package.read_zip(output)
        self.assertEqual(set(resources) - tool.package.METADATA, set(self.resources))
        self.assertTrue(all(resources[name] == body for name, body in self.resources.items()))
        verified = self.run_cli('verify', '--input', output, '--public-key', self.public)
        self.assertEqual(verified['keyId'], signed['keyId'])
        self.assertEqual(verified['packageDigest'], signed['packageDigest'])

    def test_modified_signed_zip_is_resigned_and_version_comes_from_manifest(self):
        first = self.sign()
        resources = tool.package.read_zip(Path(first['output']))
        manifest = json.loads(resources['manifest.json'])
        manifest['version'] = '0.0.14'
        resources['manifest.json'] = json.dumps(manifest).encode()
        resources['background.js'] += b'// updated\n'
        self.input.write_bytes(tool.package.zip_bytes(resources))
        output = self.root / 'updated.zip'
        updated = self.run_cli('sign', '--input', self.input, '--output', output, '--key', self.key)
        self.assertEqual(updated['version'], '0.0.14')
        self.assertNotEqual(first['packageDigest'], updated['packageDigest'])
        self.run_cli('verify', '--input', output, '--public-key', self.public)

    def test_verify_rejects_tampering_and_unknown_public_key(self):
        signed = self.sign()
        output = Path(signed['output'])
        other = self.root / 'other.pem'
        other_public = self.root / 'other.pub.pem'
        tool.package.openssl_run(self.binary, ['genpkey', '-algorithm', 'ED25519', '-out', other])
        tool.package.openssl_run(self.binary, ['pkey', '-in', other, '-pubout', '-out', other_public])
        self.run_cli('verify', '--input', output, '--public-key', other_public, ok=False)
        resources = tool.package.read_zip(output)
        resources['background.js'] += b'tampered'
        output.write_bytes(tool.package.zip_bytes(resources))
        self.run_cli('verify', '--input', output, '--public-key', self.public, ok=False)

    def test_in_place_atomically_replaces_and_preserves_unique_backups(self):
        first = self.run_cli('sign', '--input', self.input, '--key', self.key, '--in-place')
        backup = Path(first['backup'])
        self.assertEqual(self.original, backup.read_bytes())
        self.assertNotEqual(self.original, self.input.read_bytes())
        signed = self.input.read_bytes()
        second = self.run_cli('sign', '--input', self.input, '--key', self.key, '--in-place')
        self.assertNotEqual(first['backup'], second['backup'])
        self.assertEqual(signed, Path(second['backup']).read_bytes())
        self.assertEqual(self.original, backup.read_bytes())
        self.run_cli('verify', '--input', self.input, '--public-key', self.public)

    def test_in_place_uses_consistent_handle_stats_when_path_ctime_has_other_semantics(self):
        original_stat = Path.stat

        class DifferentPathCtime:
            def __init__(self, info):
                self.info = info

            def __getattr__(self, name):
                if name == 'st_ctime_ns':
                    return self.info.st_ctime_ns + 7_900_000
                return getattr(self.info, name)

        def path_stat(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            return DifferentPathCtime(info) if path == self.input else info

        args = tool.build_parser().parse_args(['sign', '--input', str(self.input), '--key', str(self.key),
                                              '--in-place', '--openssl', self.binary])
        with patch.object(Path, 'stat', path_stat):
            first = tool.sign(args)
            second = tool.sign(args)
        self.assertNotEqual(first['backup'], second['backup'])
        self.assertEqual(Path(first['backup']).read_bytes(), self.original)
        self.run_cli('verify', '--input', self.input, '--public-key', self.public)

    def test_concurrent_content_change_during_signing_is_never_overwritten(self):
        original_sign = tool.package.sign_package
        modified = self.original + b'concurrent content change'

        def signing(*args, **kwargs):
            result = original_sign(*args, **kwargs)
            self.input.write_bytes(modified)
            return result

        args = tool.build_parser().parse_args(['sign', '--input', str(self.input), '--key', str(self.key),
                                              '--in-place', '--openssl', self.binary])
        with patch.object(tool.package, 'sign_package', signing):
            with self.assertRaisesRegex(tool.ToolError, '签名期间输入 ZIP 发生变化'):
                tool.sign(args)
        self.assertEqual(self.input.read_bytes(), modified)
        self.assertFalse(list(self.root.glob('*.backup-*.zip')))

    def test_concurrent_same_bytes_identity_replacement_is_rejected(self):
        original_sign = tool.package.sign_package

        def signing(*args, **kwargs):
            result = original_sign(*args, **kwargs)
            replacement = self.root / 'replacement.zip'
            replacement.write_bytes(self.original)
            previous = self.input.stat()
            os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            os.replace(replacement, self.input)
            return result

        args = tool.build_parser().parse_args(['sign', '--input', str(self.input), '--key', str(self.key),
                                              '--in-place', '--openssl', self.binary])
        with patch.object(tool.package, 'sign_package', signing):
            with self.assertRaisesRegex(tool.ToolError, '签名期间输入 ZIP 发生变化'):
                tool.sign(args)
        self.assertEqual(self.input.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('*.backup-*.zip')))

    def test_concurrent_change_after_backup_keeps_new_input_and_unique_original_backup(self):
        original_publish = tool.publish_new
        modified = self.original + b'changed after backup publication'

        def publishing(*args, **kwargs):
            original_publish(*args, **kwargs)
            self.input.write_bytes(modified)

        args = tool.build_parser().parse_args(['sign', '--input', str(self.input), '--key', str(self.key),
                                              '--in-place', '--openssl', self.binary])
        with patch.object(tool, 'publish_new', publishing):
            with self.assertRaisesRegex(tool.ToolError, '发布前输入 ZIP 发生变化'):
                tool.sign(args)
        self.assertEqual(self.input.read_bytes(), modified)
        backups = list(self.root.glob('*.backup-*.zip'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)

    def test_invalid_private_key_or_zip_never_changes_original_or_creates_backup(self):
        wrong = self.root / 'rsa.pem'
        tool.package.openssl_run(self.binary, ['genpkey', '-algorithm', 'RSA',
            '-pkeyopt', 'rsa_keygen_bits:1024', '-out', wrong])
        self.run_cli('sign', '--input', self.input, '--key', wrong, '--in-place', ok=False)
        self.assertEqual(self.original, self.input.read_bytes())
        self.assertFalse(list(self.root.glob('*.backup-*.zip')))
        self.input.write_bytes(b'broken ZIP')
        self.run_cli('sign', '--input', self.input, '--key', self.key, '--in-place', ok=False)
        self.assertEqual(b'broken ZIP', self.input.read_bytes())
        self.assertFalse(list(self.root.glob('*.backup-*.zip')))

    def test_existing_output_and_input_alias_cannot_be_overwritten(self):
        output = self.root / 'existing.zip'
        output.write_bytes(b'preserve existing')
        self.run_cli('sign', '--input', self.input, '--key', self.key, '--output', output, ok=False)
        self.assertEqual(b'preserve existing', output.read_bytes())
        self.run_cli('sign', '--input', self.input, '--key', self.key, '--output', self.input, ok=False)
        self.assertEqual(self.original, self.input.read_bytes())

    def test_path_attacks_duplicate_entries_and_unknown_meta_are_rejected(self):
        for name in ('../escape.js', '/absolute', 'assets/../x', 'AUX.txt',
                     'Assets/other.js', 'META-INF/unknown.json'):
            with self.subTest(name=name):
                resources = {**self.resources, name: b'bad'}
                self.input.write_bytes(tool.package.zip_bytes(resources))
                self.run_cli('sign', '--input', self.input, '--key', self.key, '--in-place', ok=False)
                self.assertEqual(tool.package.zip_bytes(resources), self.input.read_bytes())
        raw = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(raw, 'w') as archive:
            warnings.simplefilter('ignore', UserWarning)
            for name, body in self.resources.items():
                archive.writestr(name, body)
            archive.writestr('background.js', b'duplicate')
        self.input.write_bytes(raw.getvalue())
        self.run_cli('sign', '--input', self.input, '--key', self.key, ok=False)

    def test_input_symlink_and_private_key_inside_package_are_rejected(self):
        linked = self.root / 'linked.zip'
        try:
            linked.symlink_to(self.input)
        except OSError:
            pass  # Windows without symlink privilege still runs the PEM check.
        else:
            self.run_cli('sign', '--input', linked, '--key', self.key, ok=False)
        self.input.write_bytes(tool.package.zip_bytes({**self.resources, 'accidental.pem': self.key.read_bytes()}))
        self.run_cli('sign', '--input', self.input, '--key', self.key, ok=False)

    def test_private_key_environment_and_missing_key(self):
        self.run_cli('sign', '--input', self.input, ok=False)
        signed = self.run_cli('sign', '--input', self.input,
                              env={'MIVPN_SIGNING_KEY_FILE': str(self.key)})
        self.assertEqual('0.0.13', signed['version'])

    def test_public_key_exports_only_public_information_and_protects_output(self):
        output = self.root / 'exported.pub.pem'
        result = self.run_cli('public-key', '--key', self.key, '--output', output)
        self.assertEqual(result['publicKeyHex'], tool.package.public_key_bytes(self.public, self.binary).hex())
        self.assertTrue(output.read_bytes().startswith(b'-----BEGIN PUBLIC KEY-----'))
        previous = output.read_bytes()
        self.run_cli('public-key', '--key', self.key, '--output', output, ok=False)
        self.assertEqual(previous, output.read_bytes())
        public_only = self.run_cli('public-key', '--public-key', self.public)
        self.assertEqual(public_only['keyId'], result['keyId'])

    def test_keygen_creates_external_exclusive_key_and_never_overwrites(self):
        private = self.root / 'new-release.pem'
        public = self.root / 'new-release.pub.pem'
        result = self.run_cli('keygen', '--key', private, '--public-key', public)
        self.assertEqual(result['keyId'], tool.package.key_id(tool.package.public_key_bytes(public, self.binary)))
        previous = private.read_bytes()
        self.run_cli('keygen', '--key', private, '--public-key', public, ok=False)
        self.assertTrue(private.read_bytes() == previous)
        if os.name != 'nt':
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

    def test_keygen_dotdot_cannot_hide_private_output_inside_checkout(self):
        checkout, outside = self.root / 'fake-checkout', self.root / 'outside'
        checkout.mkdir()
        outside.mkdir()
        disguised = outside / '..' / checkout.name / 'private.pem'
        args = tool.build_parser().parse_args(['keygen', '--key', str(disguised),
                                              '--openssl', self.binary])
        with patch.object(tool, 'ROOT', checkout):
            with self.assertRaisesRegex(tool.ToolError, '仓库外'):
                tool.keygen(args)
        self.assertFalse((checkout / 'private.pem').exists())
        self.assertFalse((checkout / 'private.pub.pem').exists())


if __name__ == '__main__':
    unittest.main()
