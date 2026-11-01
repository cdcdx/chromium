#!/usr/bin/env python3
"""Managed compatibility checks against a deterministic native ABI peer.
Run with Python 3, .NET 10 and a native C compiler (Windows: MSVC developer shell).
This does not replace Chromium/browser end-to-end acceptance.
"""
from pathlib import Path
import os
import platform
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="arupa-managed-compat-") as raw:
    work = Path(raw)
    for name in ("Program.cs", "native_shim.c"):
        shutil.copyfile(source / name, work / name)
    for path in (root / "package/package_desktop/dotnet").glob("*.cs"):
        shutil.copyfile(path, work / path.name)
    (work / "Compat.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework><OutputType>Exe</OutputType><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable><AllowUnsafeBlocks>true</AllowUnsafeBlocks></PropertyGroup></Project>')
    (work / "NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>')
    library = work / {"Windows":"shim.dll","Darwin":"libshim.dylib","Linux":"libshim.so"}[platform.system()]
    compiler = (["cl", "/LD", "native_shim.c", "/link", f"/OUT:{library}"] if os.name == "nt" else
                [os.environ.get("CC", "cc"), "-dynamiclib" if platform.system() == "Darwin" else "-shared", "-fPIC", "native_shim.c", "-o", str(library)])
    subprocess.run(compiler,cwd=work,check=True)
    managed = work / "managed"
    subprocess.run(["dotnet","build","Compat.csproj","--nologo","-v:q","-o",str(managed),
                    "-p:Platform=AnyCPU","-p:UseSharedCompilation=false","-nodeReuse:false"],cwd=work,check=True)
    subprocess.run(["dotnet",str(managed/"Compat.dll"),str(library)],cwd=work,check=True)
    legacy = library.with_name("legacy-" + library.name)
    legacy_compiler = (["cl", "/LD", "/DLEGACY_READINESS", "native_shim.c", "/link", f"/OUT:{legacy}"] if os.name == "nt" else
                       [os.environ.get("CC", "cc"), "-DLEGACY_READINESS", "-dynamiclib" if platform.system() == "Darwin" else "-shared", "-fPIC", "native_shim.c", "-o", str(legacy)])
    subprocess.run(legacy_compiler,cwd=work,check=True)
    subprocess.run(["dotnet",str(managed/"Compat.dll"),str(legacy),"--legacy-readiness"],cwd=work,check=True)
