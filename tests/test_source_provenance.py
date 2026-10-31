"""Desktop release provenance must reject source/resource drift."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_provenance as provenance


class SourceProvenanceTest(unittest.TestCase):
    def test_repository_records_tracked_and_untracked_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "source.cc").write_text("before\n")
            subprocess.run(["git", "-C", str(root), "add", "source.cc"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
            clean = provenance.repository(root)
            self.assertFalse(clean["dirty"])
            (root / "source.cc").write_text("after\n")
            changed = provenance.repository(root)
            self.assertTrue(changed["dirty"])
            subprocess.run(["git", "-C", str(root), "config", "core.abbrev", "8"], check=True)
            short_index = provenance.repository(root)["worktreeSha256"]
            subprocess.run(["git", "-C", str(root), "config", "core.abbrev", "20"], check=True)
            self.assertEqual(short_index, provenance.repository(root)["worktreeSha256"])
            (root / "new.cc").write_text("new\n")
            self.assertNotEqual(changed["worktreeSha256"],
                                provenance.repository(root)["worktreeSha256"])

    def test_kernel_stamp_blocks_changes_after_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "out"
            out.mkdir()
            identity = {"project": "arupa_desktop", "os": "linux", "arch": "x64",
                        "version": "154", "link": "static"}
            source = {"repositories": {"arupa": "base"}}
            artifacts = {"devtools_resources.pak": "base"}
            stamp = provenance.built_stamp(identity, source, artifacts)
            (out / "arupa-build.json").write_text(json.dumps(stamp))
            with patch.object(provenance, "capture", return_value=source), \
                 patch.object(provenance, "build_artifacts", return_value=artifacts):
                self.assertEqual(stamp, provenance.validate_kernel_stamp(root, out, "linux", identity))
            with patch.object(provenance, "capture", return_value={"repositories": {"arupa": "changed"}}), \
                 patch.object(provenance, "build_artifacts", return_value=artifacts):
                with self.assertRaisesRegex(RuntimeError, "sources changed"):
                    provenance.validate_kernel_stamp(root, out, "linux", identity)
            with patch.object(provenance, "capture", return_value=source), \
                 patch.object(provenance, "build_artifacts", return_value={"devtools_resources.pak": "changed"}):
                with self.assertRaisesRegex(RuntimeError, "resources changed"):
                    provenance.validate_kernel_stamp(root, out, "linux", identity)

    def test_delivery_hash_gate_rejects_modified_and_added_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "kernel.bin"
            file.write_bytes(b"sealed")
            sums = root / "SHA256SUMS.txt"
            sums.write_text(f"{provenance.file_sha256(file)}  kernel.bin\n")
            self.assertEqual(provenance.file_sha256(sums),
                             provenance.verify_delivery_checksums(root))
            file.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                provenance.verify_delivery_checksums(root)
            file.write_bytes(b"sealed")
            (root / "late.dll").write_bytes(b"late")
            with self.assertRaisesRegex(RuntimeError, "file list changed"):
                provenance.verify_delivery_checksums(root)

    def test_delivery_hash_gate_tolerates_dotnet_build_intermediates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dotnet").mkdir()
            file = root / "dotnet/ArupaKernel.dll"
            file.write_bytes(b"sealed")
            sums = root / "SHA256SUMS.txt"
            sums.write_text(f"{provenance.file_sha256(file)}  dotnet/ArupaKernel.dll\n")
            # IDE/设计时构建留在交付里的中间产物不属于交付内容，不该被算成新增文件。
            intermediate = root / "dotnet/obj/Debug/net10.0/ArupaKernel.AssemblyInfo.cs"
            intermediate.parent.mkdir(parents=True)
            intermediate.write_text("// generated\n")
            (root / "dotnet/bin/Debug/net10.0").mkdir(parents=True)
            self.assertEqual(provenance.file_sha256(sums),
                             provenance.verify_delivery_checksums(root))
            # 中间产物路径之外的任何新增文件仍然拒绝。
            (root / "dotnet/ArupaExtra.cs").write_text("// extra\n")
            with self.assertRaisesRegex(RuntimeError, "file list changed"):
                provenance.verify_delivery_checksums(root)


if __name__ == "__main__":
    unittest.main()
