#!/usr/bin/env python3
"""Exercise the Android AAR loader gate with real NDK-linked ELF fixtures."""
import argparse
import json
from pathlib import Path
import subprocess
import zipfile
import android_native_compatibility as gate

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = gate.ROOT
    dest = args.output.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    compiler = root / "src/third_party/llvm-build/Release+Asserts/bin/clang"
    sysroot = gate.NDK / "toolchains/llvm/prebuilt/linux-x86_64/sysroot"
    (dest / "api26.gn").write_text("default_min_sdk_version = 26\nandroid_ndk_api_level = 26\n")
    (dest / "api29.gn").write_text("default_min_sdk_version = 29\nandroid_ndk_api_level = 29\n")
    cases = [
        ("api26-valid", "int probe(void) { return 7; }", 26, 16384, [], True, "", "x86_64", "api26"),
        ("weak-optional-valid", "extern int optional(void) __attribute__((weak)); int probe(void) { return optional ? optional() : -1; }",
         26, 16384, [], True, "", "x86_64", "api26"),
        ("new-api-strong-rejected", 'extern int android_res_nquery(unsigned long,const char*,int,int,unsigned); int probe(void) { return android_res_nquery(0,"example.invalid",1,1,0); }',
         29, 16384, ["-landroid"], False, "strong imports", "x86_64", "api26"),
        ("binder-needed-rejected", "int probe(void) { return 7; }",
         29, 16384, ["-Wl,--no-as-needed", "-lbinder_ndk"], False, "libbinder_ndk.so", "x86_64", "api26"),
        ("4kb-align-rejected", "int probe(void) { return 7; }",
         26, 4096, [], False, "16KB alignment", "x86_64", "api26"),
        ("native-tls-rejected", "_Thread_local int value; int probe(void) { return ++value; }",
         29, 16384, [], False, "native ELF TLS", "x86_64", "api26"),
        ("minimum-mismatch-rejected", "int probe(void) { return 7; }",
         26, 16384, [], False, "minimum API mismatch", "x86_64", "api29"),
        ("wrong-abi-rejected", "int probe(void) { return 7; }",
         26, 16384, [], False, "does not match", "aarch64", "api26"),
    ]
    results = []
    for name, source, api, alignment, extra, valid, reason, arch, gn in cases:
        code = dest / (name + ".c")
        library = dest / (name + ".so")
        archive = dest / (name + ".aar")
        code.write_text(source)
        triple = "x86_64-linux-android" if arch == "x86_64" else "aarch64-linux-android"
        subprocess.run([str(compiler), "--target=" + triple + str(api),
                        "--sysroot=" + str(sysroot), "-shared", "-fPIC", "-nostdlib",
                        "-fuse-ld=lld", "-L" + str(sysroot / "usr/lib" / triple / str(api)),
                        "-Wl,-z,max-page-size=" + str(alignment), str(code), "-lc",
                        *extra, "-o", str(library)], check=True, capture_output=True)
        manifest = '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><uses-sdk android:minSdkVersion="26"/></manifest>'
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("AndroidManifest.xml", manifest)
            output.write(library, "jni/x86_64/libfixture.so")
        report = gate.check(archive, dest / (gn + ".gn"))
        assert report["valid"] == valid, (name, report)
        assert not reason or reason in "\n".join(report["errors"]), (name, report)
        (dest / (name + ".json")).write_text(json.dumps(report, indent=2))
        results.append({"case": name, "passed": True})
        print(name + ": passed", flush=True)
    (dest / "results.json").write_text(json.dumps(results, indent=2))
    print(str(len(results)) + " real ELF fixture cases passed")

if __name__ == "__main__":
    main()
