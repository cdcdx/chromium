"""Regression coverage for complete MiVPN inventories and Ed25519 signing."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('pack_mivpn', Path(__file__).resolve().parents[1] / 'pack_mivpn.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required')
class PackageSigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='mivpn-signing-tests-')
        cls.root = Path(cls.temp.name)
        cls.binary = shutil.which('openssl')
        cls.key = cls.root / 'release.pem'
        cls.other_key = cls.root / 'other.pem'
        for key in (cls.key, cls.other_key):
            subprocess.run([cls.binary, 'genpkey', '-algorithm', 'ED25519', '-out', str(key)],
                           check=True, capture_output=True)
        cls.public = subprocess.run([cls.binary, 'pkey', '-in', str(cls.key), '-pubout', '-outform', 'DER'],
                                    check=True, capture_output=True).stdout

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.trust = patch.object(pack, 'PACKAGE_PUBLIC_DER', self.public)
        self.trust.start()
        self.addCleanup(self.trust.stop)
        self.manifest = {'manifest_version': 3, 'name': 'fixture', 'version': '0.0.38'}
        self.files = {'manifest.json': (json.dumps(self.manifest).encode(), 0o644),
                      'engine/win-x64/mivpn-browser.dll': (b'windows engine', 0o644),
                      'engine/mac-arm64/mivpn-browser.dylib': (b'mac arm engine', 0o644)}

    def sign(self, key=None, abi=None):
        return pack.update_package_metadata(self.files, self.manifest, key or self.key, self.binary, abi)

    def verify(self):
        return pack.update_package_metadata(self.files, self.manifest, None, self.binary)

    def test_repair_inventory_after_adding_both_x64_engines(self):
        self.sign(abi=63)
        self.files['engine/mac-x64/mivpn-browser.dylib'] = (b'mac x64 engine', 0o644)
        self.files['engine/linux-x64/libmivpn-browser.so'] = (b'linux x64 engine', 0o644)
        before = {name: entry for name, entry in self.files.items() if name not in pack.PACKAGE_METADATA}
        with self.assertRaisesRegex(ValueError, '签名清单缺失或与资源不一致'):
            self.verify()
        result = self.sign()
        self.assertEqual(result['minKernelAbiMinor'], 63)
        self.assertEqual(result['signedResourceCount'], 5)
        rows = json.loads(self.files[pack.INVENTORY][0])['files']
        self.assertEqual([row['path'] for row in rows], sorted(before))
        self.assertEqual(before, {name: entry for name, entry in self.files.items() if name in before})
        self.assertEqual(self.verify()['action'], 'verified')

    def test_added_changed_and_removed_resources_are_rehashed(self):
        self.sign()
        del self.files['engine/mac-arm64/mivpn-browser.dylib']
        self.files['engine/win-x64/mivpn-browser.dll'] = (b'new windows engine', 0o644)
        self.files['engine/future-platform/runtime.bin'] = (b'future engine', 0o644)
        self.sign()
        rows = json.loads(self.files[pack.INVENTORY][0])['files']
        self.assertEqual({r['path'] for r in rows}, set(self.files) - pack.PACKAGE_METADATA)
        self.verify()

    def test_valid_package_can_be_repacked_without_private_key(self):
        self.sign()
        before = dict(self.files)
        self.assertEqual(self.verify()['action'], 'verified')
        self.assertEqual(before, self.files)

    def test_tampered_signature_is_rejected_without_private_key(self):
        self.sign()
        self.files[pack.PACKAGE_SIGNATURE] = (b'\0' * 64, 0o644)
        with self.assertRaisesRegex(ValueError, '内层签名验证失败'):
            self.verify()

    def test_wrong_publisher_cannot_resign(self):
        before = dict(self.files)
        with self.assertRaisesRegex(ValueError, '当前受信的 Ed25519'):
            self.sign(key=self.other_key)
        self.assertEqual(before, self.files)

    def test_unsigned_package_requires_private_key(self):
        with self.assertRaisesRegex(ValueError, '--package-key'):
            self.verify()

    def test_manifest_version_is_reflected_in_new_inventory(self):
        self.sign()
        self.manifest['version'] = '0.0.39'
        self.files['manifest.json'] = (json.dumps(self.manifest).encode(), 0o644)
        self.sign()
        self.assertEqual(json.loads(self.files[pack.INVENTORY][0])['version'], '0.0.39')
        self.verify()


if __name__ == '__main__':
    unittest.main()
