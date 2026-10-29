"""Deterministic desktop source/build identity for release manifests.

Git commits alone are insufficient in these workspaces: the Chromium overlay
and product fixes may be uncommitted. Record the exact tracked diff and every
non-ignored untracked file without depending on platform-specific CRLF bytes of
the committed DevTools patch (its Git blob is the authority).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


def _git(path: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(path), *args],
                            capture_output=True, check=True)
    return result.stdout.strip() if args[:1] == ("rev-parse",) else result.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repository(path: Path) -> dict:
    if not path.is_dir():
        raise RuntimeError(f"missing source repository: {path}")
    head = _git(path, "rev-parse", "HEAD").decode("ascii")
    tree = _git(path, "rev-parse", "HEAD^{tree}").decode("ascii")
    digest = hashlib.sha256()
    # The default abbreviated blob IDs depend on each machine's Git config
    # (and repository object count), so identical edits can hash differently.
    # These workspaces contain nested checkouts whose `git diff` command can
    # fail with "this operation must be run in a work tree" even though
    # status, diff-files, and the index are available.  Compose the same
    # HEAD-to-worktree change as HEAD-to-index plus index-to-worktree so both
    # staged and unstaged edits remain part of the provenance hash.
    diff = (_git(path, "diff-index", "--cached", "--binary", "--full-index",
                 "HEAD", "--") +
            _git(path, "diff-files", "--binary", "--full-index"))
    digest.update(diff)
    untracked = _git(path, "ls-files", "--others", "--exclude-standard", "-z")
    names = sorted(name for name in untracked.split(b"\0") if name)
    for name in names:
        relative = name.decode("utf-8", errors="surrogateescape")
        source = path / relative
        digest.update(name + b"\0")
        digest.update(file_sha256(source).encode("ascii") if source.is_file() else b"not-a-file")
    dirty = bool(diff or names)
    return {"commit": head, "tree": tree, "dirty": dirty,
            "worktreeSha256": digest.hexdigest() if dirty else None}


def abi_version(root: Path) -> str:
    header = (root / "arupa_desktop/public/arupa_kernel_capi.h").read_text(encoding="utf-8")
    major = re.search(r"^#define ARUPA_KERNEL_ABI_MAJOR (\d+)$", header, re.M)
    minor = re.search(r"^#define ARUPA_KERNEL_ABI_MINOR (\d+)$", header, re.M)
    if not major or not minor:
        raise RuntimeError("cannot read desktop C ABI version")
    return f"{major[1]}.{minor[1]}"


def capture(root: Path, gn_args: Path | None = None) -> dict:
    root = root.resolve()
    repos = {name: repository(path) for name, path in (
        ("build", root), ("chromium", root / "src"),
        ("arupa", root / "arupa_desktop"), ("nomad", root / "nomad_desktop"),
        ("devtools", root / "src/third_party/devtools-frontend/src"))}
    patch = _git(root / "arupa_desktop", "rev-parse",
                 "HEAD:patches/chromium-devtools-external-dock-154.patch").decode("ascii")
    return {"schema": 1, "repositories": repos, "devtoolsPatchGitBlob": patch,
            "abi": abi_version(root),
            "gnArgsSha256": file_sha256(gn_args) if gn_args is not None else None}


def build_artifacts(out: Path, target_os: str) -> dict[str, str]:
    library = {"mac": "libarupa_kernel.dylib", "linux": "libarupa_kernel.so",
               "win": "arupa_kernel.dll"}[target_os]
    helper = "arupa_render.exe" if target_os == "win" else "arupa_render"
    names = (library, helper, "content_shell.pak", "devtools_resources.pak", "icudtl.dat")
    missing = [name for name in names if not (out / name).is_file()]
    if missing:
        raise RuntimeError(f"missing desktop build artifacts: {', '.join(missing)}")
    return {name: file_sha256(out / name) for name in names}


def built_stamp(identity: dict, source: dict, artifacts: dict) -> dict:
    return {"schema": 2, "identity": identity, "source": source,
            "artifacts": artifacts, "builtAtUtc": datetime.now(timezone.utc).isoformat()}


def validate_kernel_stamp(root: Path, out: Path, target_os: str, identity: dict) -> dict:
    stamp_path = out / "arupa-build.json"
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(f"missing/invalid successful build provenance: {stamp_path}") from exc
    if stamp.get("schema") != 2 or stamp.get("identity") != identity:
        raise RuntimeError(f"kernel build provenance identity mismatch: {stamp_path}")
    if stamp["source"] != capture(root, out / "args.gn"):
        raise RuntimeError("desktop sources changed since kernel build; rebuild before packaging")
    if stamp["artifacts"] != build_artifacts(out, target_os):
        raise RuntimeError("kernel/resources changed since build; rebuild before packaging")
    return stamp


def kernel_delivery_provenance(path: Path) -> dict:
    sums_sha256 = verify_delivery_checksums(path)
    stamp = path / "source-provenance.json"
    if not stamp.is_file():
        raise RuntimeError(f"kernel delivery lacks source provenance: {stamp}")
    value = json.loads(stamp.read_text(encoding="utf-8"))
    if value.get("schema") != 2 or "source" not in value:
        raise RuntimeError(f"invalid kernel delivery source provenance: {stamp}")
    return {"pathName": path.name, "stampSha256": file_sha256(stamp),
            "sha256SumsSha256": sums_sha256,
            "source": value["source"], "kernelArtifacts": value["artifacts"]}


def verify_delivery_checksums(path: Path) -> str:
    """Check every shipped regular file, including files added after sealing."""
    sums = path / "SHA256SUMS.txt"
    if not sums.is_file():
        raise RuntimeError(f"kernel delivery lacks SHA256SUMS.txt: {sums}")
    listed: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if not match:
            raise RuntimeError(f"invalid SHA256SUMS entry in {sums}: {line[:100]}")
        name = match[2]
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name in listed:
            raise RuntimeError(f"unsafe/duplicate SHA256SUMS path: {name}")
        listed[name] = match[1].lower()
    actual = {p.relative_to(path).as_posix() for p in path.rglob("*")
              if p.is_file() and not p.is_symlink() and
              p.relative_to(path).as_posix() not in ("SHA256SUMS.txt", "MANIFEST.md")}
    if set(listed) != actual:
        added = sorted(actual - set(listed))[:5]
        missing = sorted(set(listed) - actual)[:5]
        raise RuntimeError(f"kernel delivery file list changed after sealing: added={added}, missing={missing}")
    for name, expected in listed.items():
        if file_sha256(path / name) != expected:
            raise RuntimeError(f"kernel delivery SHA256 mismatch after sealing: {name}")
    return file_sha256(sums)
