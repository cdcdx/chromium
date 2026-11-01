"""Reject unsafe probe deliveries by inspecting metadata, without launching them."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/mivpn_probe_guard.ps1"
PWSH = shutil.which("pwsh")
DOTNET = shutil.which("dotnet")


@unittest.skipUnless(PWSH and DOTNET, "PowerShell 7 and .NET SDK required")
class MivpnProbeGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mivpn-probe-guard-")
        cls.work = Path(cls.temporary.name)
        (cls.work / "Fixture.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
            '<AssemblyName>NomadBrowser</AssemblyName></PropertyGroup></Project>')
        (cls.work / "NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>')
        source = ('namespace NomadBrowser.Avalonia {\n#if DEBUG\n'
                  'internal static class NativeMivpnProbeProfile { internal static void Prepare(string[] args) {} }\n'
                  '#endif\npublic sealed class Client {} }')
        cls.assemblies = {}
        for name, configuration, text in (
            ("debug", "Debug", source), ("release", "Release", source),
            ("old-debug", "Debug", source.replace("NativeMivpnProbeProfile", "OldClient")),
            ("no-prepare", "Debug", source.replace("Prepare(", "Other(")),
        ):
            (cls.work / "Fixture.cs").write_text(text)
            output = cls.work / name
            command = [DOTNET, "build", "Fixture.csproj", "-c", configuration, "-o", str(output),
                       "--nologo", "-v:q", "-p:Platform=AnyCPU", "-p:UseSharedCompilation=false",
                       "-nodeReuse:false", "-m:1"]
            result = subprocess.run(command, cwd=cls.work, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            cls.assemblies[name] = output / "NomadBrowser.dll"

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def payload(self, kind="debug", variant="debug"):
        payload = self.work / self.id().split(".")[-1]
        payload.mkdir()
        assembly = payload / "NomadBrowser.dll"
        shutil.copy2(self.assemblies[kind], assembly)
        (payload / "NomadBrowser.exe").write_bytes(b"must never execute")
        (payload / "build-manifest.json").write_text(json.dumps({
            "identity": {"project": "nomad_desktop", "os": "win", "variant": variant},
            "files": {"NomadBrowser.dll": hashlib.sha256(assembly.read_bytes()).hexdigest()},
        }))
        return payload

    def inspect(self, payload):
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        script = (f". {quote(GUARD)}; try {{ Assert-MivpnProbePayload -PayloadRoot {quote(payload)}; exit 0 }} "
                  "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return subprocess.run([PWSH, "-NoProfile", "-EncodedCommand", encoded],
                              capture_output=True, text=True, timeout=30)

    def test_current_debug_harness_is_accepted(self):
        result = self.inspect(self.payload())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_release_identity_is_rejected(self):
        result = self.inspect(self.payload(variant="release"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-Debug", result.stderr)

    def test_release_assembly_cannot_be_relabelled_debug(self):
        result = self.inspect(self.payload(kind="release"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isolated-profile", result.stderr)

    def test_old_debug_client_is_rejected(self):
        result = self.inspect(self.payload(kind="old-debug"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isolated-profile", result.stderr)

    def test_type_without_prepare_is_rejected(self):
        result = self.inspect(self.payload(kind="no-prepare"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isolated-profile", result.stderr)

    def test_stale_manifest_is_rejected(self):
        payload = self.payload()
        with (payload / "NomadBrowser.dll").open("ab") as stream:
            stream.write(b"changed after sealing")
        result = self.inspect(payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sealed delivery", result.stderr)

    def test_missing_manifest_is_rejected(self):
        payload = self.payload()
        (payload / "build-manifest.json").unlink()
        self.assertNotEqual(self.inspect(payload).returncode, 0)

    def test_script_rejects_release_before_creating_profile(self):
        payload = self.payload(kind="release", variant="release")
        profile = self.work / "must-not-create-profile"
        archive = self.work / "empty.zip"
        with zipfile.ZipFile(archive, "w"):
            pass
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(ROOT / "scripts/probe-mivpn-native.ps1"),
                                 "-PayloadRoot", str(payload), "-ProbeRoot", str(profile),
                                 "-PluginZip", str(archive)], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(profile.exists(), result.stdout + result.stderr)

    def test_connection_mode_rejects_invalid_endpoint_before_creating_profile(self):
        payload = self.payload()
        profile = self.work / "invalid-endpoint-profile"
        archive = self.work / "invalid-endpoint.zip"
        with zipfile.ZipFile(archive, "w"):
            pass
        for endpoint in ("", "http://example.invalid/", "https://user:secret@example.invalid/",
                         "https://localhost/", "https://example.invalid/#fragment"):
            with self.subTest(endpoint=endpoint):
                command = [PWSH, "-NoProfile", "-File", str(ROOT / "scripts/probe-mivpn-native.ps1"),
                           "-PayloadRoot", str(payload), "-ProbeRoot", str(profile),
                           "-PluginZip", str(archive), "-VerifyConnections"]
                if endpoint:
                    command.extend(["-EgressUrl", endpoint])
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Connection verification requires", result.stderr)
                self.assertFalse(profile.exists(), result.stdout + result.stderr)


@unittest.skipUnless(PWSH, "PowerShell 7 required")
class MivpnConnectionEvidenceTests(unittest.TestCase):
    @staticmethod
    def complete():
        rows = [
            ("reality_tcp", "connect", None), ("reality_tcp", "egress", True),
            ("hysteria2", "switch", None), ("hysteria2", "egress", True),
            ("none", "disconnect", None),
        ]
        return {"connectionsVerified": True, "connectionResults": [
            {"protocol": protocol, "stage": stage, "passed": True, "egressMatched": matched, "code": "OK"}
            for protocol, stage, matched in rows]}

    def accepted(self, report):
        data = base64.b64encode(json.dumps(report).encode("utf-8")).decode("ascii")
        guard = str(GUARD).replace("'", "''")
        script = (f". '{guard}'; $data=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{data}')) | ConvertFrom-Json; "
                  "if (Test-MivpnConnectionEvidence -Report $data) { exit 0 } else { exit 2 }")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run([PWSH, "-NoProfile", "-EncodedCommand", encoded], capture_output=True, text=True, timeout=30)
        self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
        return result.returncode == 0

    def test_both_protocols_egress_and_disconnect_are_accepted(self):
        self.assertTrue(self.accepted(self.complete()))

    def test_sdk_only_report_is_not_connection_evidence(self):
        self.assertFalse(self.accepted({"sdkAvailable": True}))

    def test_missing_disconnect_is_rejected(self):
        report = self.complete()
        report["connectionResults"].pop()
        self.assertFalse(self.accepted(report))

    def test_unmatched_egress_is_rejected(self):
        report = self.complete()
        report["connectionResults"][3]["egressMatched"] = False
        self.assertFalse(self.accepted(report))

    def test_failed_extra_step_is_rejected(self):
        report = self.complete()
        report["connectionResults"].append({"passed": False})
        self.assertFalse(self.accepted(report))

    def test_duplicate_protocol_cannot_replace_second_protocol(self):
        report = self.complete()
        report["connectionResults"][2]["protocol"] = "reality_tcp"
        self.assertFalse(self.accepted(report))

    def test_false_summary_is_rejected(self):
        report = self.complete()
        report["connectionsVerified"] = False
        self.assertFalse(self.accepted(report))

    def test_string_boolean_is_rejected(self):
        report = self.complete()
        report["connectionsVerified"] = "true"
        self.assertFalse(self.accepted(report))


if __name__ == "__main__":
    unittest.main()
