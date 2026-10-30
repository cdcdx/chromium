#!/usr/bin/env python3
"""Install a verified dual-ABI Arupa delivery into the Android host checkout.

The runtime manifest is written last. Existing host assets are backed up under
nomad_android/.scratch/kernel-delivery-backup/<old-id>/ before replacement.
"""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        pending = Path(temp.name)
    try:
        shutil.copy2(source, pending)
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path, help="Unzipped arupa-android-*-static-N delivery")
    parser.add_argument("host", type=Path, help="nomad_android checkout")
    args = parser.parse_args()
    stage = args.stage.resolve()
    host = args.host.resolve()
    capability_file = stage / "capability-manifest.json"
    capability = json.loads(capability_file.read_text(encoding="utf-8"))
    delivery_id = capability["delivery_id"]
    locked = capability["locked_sha256"]
    manifest_file = host / "tools/ci/runtime-manifest.json"
    runtime = json.loads(manifest_file.read_text(encoding="utf-8"))
    old_id = runtime["kernelDeliveryId"]

    transfers: dict[Path, Path] = {}
    for arch in ("arm64", "x64"):
        kernel = stage / "kernel" / arch
        for name in ("arupa-kernel.aar", "arupa_kernel.pak", "snapshot_blob_64.bin"):
            source = kernel / name
            expected = locked.get(f"{arch}/{name}")
            if not source.is_file() or digest(source) != expected:
                raise SystemExit(f"Delivery file missing or changed: {source}")
            if name == "arupa-kernel.aar":
                target = host / "app/libs/kernel" / arch / name
            else:
                target = host / "app/kernel-assets" / arch / name
            transfers[target] = source
        snapshot = kernel / "v8_context_snapshot_64.bin"
        if snapshot.is_file():
            expected = locked.get(f"{arch}/v8_context_snapshot_64.bin")
            if digest(snapshot) != expected:
                raise SystemExit(f"Snapshot digest mismatch: {snapshot}")
            transfers[host / "app/kernel-assets" / arch / snapshot.name] = snapshot
    arm_resource = stage / "kernel/arm64/arupa_kernel_resources.apk"
    x64_resource = stage / "kernel/x64/arupa_kernel_resources.apk"
    for arch, source in (("arm64", arm_resource), ("x64", x64_resource)):
        if digest(source) != locked.get(f"{arch}/arupa_kernel_resources.apk"):
            raise SystemExit(f"Resource APK digest mismatch: {source}")
    if digest(arm_resource) != digest(x64_resource):
        raise SystemExit("Cross-ABI Chromium resource APK mismatch")
    icu = stage / "kernel/arm64/icudtl.dat"
    if digest(icu) != locked.get("arm64/icudtl.dat"):
        raise SystemExit("arm64 ICU digest mismatch")
    if digest(icu) != digest(stage / "kernel/x64/icudtl.dat"):
        raise SystemExit("Cross-ABI ICU data mismatch")
    transfers[host / "app/src/main/assets/icudtl.dat"] = icu
    transfers[host / "tools/ci/kernel-capability-manifest.json"] = capability_file

    backup = host / ".scratch/kernel-delivery-backup" / old_id.replace("/", "_")
    for target in transfers:
        if target.is_file():
            saved = backup / target.relative_to(host)
            if not saved.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)

    for target, source in transfers.items():
        atomic_copy(source, target)

    runtime["kernelDeliveryId"] = delivery_id
    runtime["kernelDeliveryIds"] = {"arm64": delivery_id, "x64": delivery_id}
    for relative in list(runtime["files"]):
        if relative.startswith(("app/libs/kernel/", "app/kernel-assets/")) or relative == "app/src/main/assets/icudtl.dat":
            path = host / relative
            if not path.is_file():
                raise SystemExit(f"Host runtime asset missing after install: {path}")
            runtime["files"][relative] = digest(path)
    runtime["kernelResourceApkSha256"] = digest(arm_resource)
    runtime["kernelCapabilityManifestSha256"] = digest(
        host / runtime["kernelCapabilityManifest"])
    output = json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=manifest_file.parent, delete=False
    ) as temp:
        temp.write(output)
        pending = Path(temp.name)
    os.replace(pending, manifest_file)
    print(f"Installed {delivery_id} in {host}; previous assets saved in {backup}")


if __name__ == "__main__":
    main()
