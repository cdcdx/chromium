#!/usr/bin/env python3
"""Validate the native loader contract of an Android kernel AAR before delivery."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NDK = ROOT / "src/third_party/android_toolchain/ndk"
ABIS = {"arm64-v8a": ("aarch64-linux-android", 183), "x86_64": ("x86_64-linux-android", 62)}
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

def readelf_tool() -> str:
    for name in ("llvm-readelf", "readelf"):
        if tool := shutil.which(name):
            return tool
    for tool in sorted((NDK / "toolchains/llvm/prebuilt").glob("*/bin/llvm-readelf")):
        if tool.is_file():
            return str(tool)
    raise ValueError("readelf/llvm-readelf is required for the Android loader gate")

def symbols(tool: str, path: Path) -> tuple[set[str], set[str], set[str]]:
    output = subprocess.check_output([tool, "--dyn-syms", "-W", str(path)], text=True)
    strong, weak, exported = set(), set(), set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[4] not in ("GLOBAL", "WEAK"):
            continue
        name = fields[7].split("@")[0]
        if fields[6] == "UND":
            (strong if fields[4] == "GLOBAL" else weak).add(name)
        else:
            exported.add(name)
    return strong, weak, exported

def elf_loads(data: bytes) -> tuple[int, list[dict], bool]:
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("expected a little-endian ELF64 library")
    machine = struct.unpack_from("<H", data, 18)[0]
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    loads = []
    has_tls = False
    for i in range(count):
        entry = offset + i * size
        segment_type = struct.unpack_from("<I", data, entry)[0]
        has_tls |= segment_type == 7
        if segment_type == 1:
            loads.append({"offset": struct.unpack_from("<Q", data, entry + 8)[0],
                          "vaddr": struct.unpack_from("<Q", data, entry + 16)[0],
                          "align": struct.unpack_from("<Q", data, entry + 48)[0]})
    if not loads:
        raise ValueError("ELF has no LOAD segments")
    return machine, loads, has_tls

def sdk_value(args: str, key: str, default: int) -> int:
    values = re.findall(r"^\s*" + re.escape(key) + r"\s*=\s*(\d+)\s*(?:#.*)?$", args, re.M)
    if len(values) > 1:
        raise ValueError("duplicate GN argument: " + key)
    return int(values[0]) if values else default

def check(aar: Path, args: Path, sysroot: Path | None = None) -> dict:
    tool = readelf_tool()
    gn = args.read_text()
    # Chromium's default is currently 29; legacy deliveries must opt in
    # explicitly, so an accidentally omitted API 26 setting is rejected.
    minimum = sdk_value(gn, "default_min_sdk_version", 29)
    ndk_api = sdk_value(gn, "android_ndk_api_level", minimum)
    result = {"aarSha256": hashlib.sha256(aar.read_bytes()).hexdigest(),
              "minimumAndroidApi": minimum, "ndkApi": ndk_api, "libraries": [], "errors": []}
    errors = result["errors"]
    with zipfile.ZipFile(aar) as archive, tempfile.TemporaryDirectory(prefix="arupa-elf-gate-") as temp:
        manifest = ET.fromstring(archive.read("AndroidManifest.xml"))
        uses_sdk = manifest.find("uses-sdk")
        if uses_sdk is None:
            raise ValueError("AAR must declare uses-sdk/minSdkVersion")
        declared = int(uses_sdk.attrib[ANDROID_NS + "minSdkVersion"])
        result["manifestMinSdk"] = declared
        if declared != minimum or ndk_api != minimum:
            errors.append(f"minimum API mismatch: AAR={declared}, GN={minimum}, NDK={ndk_api}")
        entries = [n for n in archive.namelist() if n.startswith("jni/") and n.endswith(".so")]
        if not entries:
            errors.append("AAR has no native libraries")
        for name in entries:
            parts = name.split("/")
            if len(parts) != 3 or parts[1] not in ABIS:
                errors.append("unsupported native entry: " + name)
                continue
            abi = parts[1]
            triplet, expected_machine = ABIS[abi]
            data = archive.read(name)
            path = Path(temp) / abi / parts[2]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            machine, loads, has_tls = elf_loads(data)
            strong, weak, _ = symbols(tool, path)
            dynamic = subprocess.check_output([tool, "-d", str(path)], text=True)
            needed = [re.search(r"\[([^\]]+)\]", line).group(1)
                      for line in dynamic.splitlines() if "(NEEDED)" in line]
            library = {"entry": name, "sha256": hashlib.sha256(data).hexdigest(),
                       "needed": needed, "loadSegments": loads, "weakImports": sorted(weak)}
            library["usesElfTls"] = has_tls
            result["libraries"].append(library)
            if minimum < 29 and has_tls:
                errors.append(name + ": native ELF TLS requires API 29; use emulated TLS")
            if minimum < 28 and re.search(r"\((?:ANDROID_)?RELR(?:SZ|ENT)?\)", dynamic):
                errors.append(name + ": RELR relocations require API 28")
            if machine != expected_machine:
                errors.append(f"{name}: ELF machine {machine} does not match {abi}")
            if any(s["align"] < 16384 or (s["vaddr"] - s["offset"]) % 16384 for s in loads):
                errors.append(name + ": LOAD segments do not support 16KB alignment")
            platform = (sysroot if sysroot is not None else
                        NDK / "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib") / triplet / str(minimum)
            if not platform.is_dir() and sysroot is None:
                candidates = list((NDK / "toolchains/llvm/prebuilt").glob(
                    "*/sysroot/usr/lib/" + triplet + "/" + str(minimum)))
                if candidates:
                    platform = candidates[0]
            if not platform.is_dir():
                raise ValueError("missing NDK API inventory: " + str(platform))
            available = set()
            for dependency in needed:
                stub = platform / dependency
                if not stub.is_file():
                    errors.append(f"{name}: {dependency} is not a public API {minimum} system library")
                    continue
                available.update(symbols(tool, stub)[2])
            missing = sorted(strong - available)
            library["missingStrongImports"] = missing
            if missing:
                errors.append(f"{name}: {len(missing)} strong imports unavailable at API {minimum}: " + ", ".join(missing))
    result["valid"] = not errors
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aar", required=True, type=Path)
    parser.add_argument("--args", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    try:
        report = check(options.aar, options.args)
    except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError, KeyError,
            struct.error, subprocess.CalledProcessError) as error:
        report = {"valid": False, "errors": [str(error)]}
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["valid"]:
        for error in report["errors"]:
            print("Android native compatibility: " + error)
        return 1
    print("Android native compatibility: API " + str(report["minimumAndroidApi"]) + ", 16KB ELF, " +
          str(len(report["libraries"])) + " libraries verified")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
