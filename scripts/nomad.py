"""Browser build backends. GN remains the responsibility of the Arupa targets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET

import fetch as F
import toolchains


def prepare_dotnet(args, repo, project=None):
    executable = args.dotnet or toolchains.dotnet_executable(F.load_config())
    executable = os.path.expanduser(executable)
    if not F.DRY_RUN:
        resolved = shutil.which(executable)
        if not resolved:
            F.err('未找到 dotnet；请先 fetch dotnet，或用 --dotnet / dotnet_path 指定 .NET SDK')
        # Run in the repository so global.json participates in SDK selection.
        result = subprocess.run([resolved, '--version'], cwd=repo, capture_output=True, text=True)
        if result.returncode or not result.stdout.strip():
            F.err('没有可用的项目 .NET SDK（仅安装 Runtime 不够）；请检查 global.json。\n' + result.stderr.strip())
        if project and project.stat().st_size:
            frameworks = [element.text or '' for element in ET.parse(project).iter()
                          if element.tag.rsplit('}', 1)[-1] in ('TargetFramework', 'TargetFrameworks')]
            required = [tuple(map(int, match)) for value in frameworks
                        for match in re.findall(r'\bnet(\d+)\.(\d+)', value)]
            actual = re.match(r'(\d+)\.(\d+)', result.stdout.strip())
            if required and (not actual or tuple(map(int, actual.groups())) < max(required)):
                F.err(f'.NET SDK {result.stdout.strip()} 不支持项目目标 {", ".join(frameworks)}；'
                      '请先 bash fetch.sh dotnet（Windows 使用 .\\fetch.ps1 dotnet），或用 --dotnet 指定兼容 SDK')
        executable = resolved
        sdk_root = str(Path(resolved).resolve().parent)
        os.environ['DOTNET_ROOT'] = sdk_root
        os.environ['PATH'] = sdk_root + os.pathsep + os.environ.get('PATH', '')
        F.log(f'.NET SDK: {result.stdout.strip()} ({executable})')
    return executable


def prepare_web_tools(web):
    """Use npm's semver implementation; keep node and npm from the same install."""
    metadata = json.loads((web / 'package.json').read_text(encoding='utf-8'))
    required = metadata.get('engines', {}).get('node', '*')
    if F.DRY_RUN:
        F.log(f'(dry-run) 检查 Node.js/npm，Node 要求 {required}')
        return 'npm.cmd' if F.IS_WIN else 'npm'
    configured = F.cget(F.load_config(), 'node_path')
    current = shutil.which('node')
    candidates = [Path(configured).expanduser()] if configured else ([Path(current)] if current else [])
    if not configured:
        nvm = Path(os.environ.get('NVM_DIR', str(Path.home() / '.nvm')))
        def version_key(path):
            return tuple(map(int, re.findall(r'\d+', path.parent.parent.name)))
        candidates += sorted((nvm / 'versions/node').glob('v*/bin/node'), key=version_key, reverse=True)
    failures = []
    for node in dict.fromkeys(candidates):
        npm = node.parent / ('npm.cmd' if F.IS_WIN else 'npm')
        if not node.is_file() or not npm.is_file():
            failures.append(f'{node}: 缺少配套 node/npm')
            continue
        # npm.cmd is a launcher, while POSIX npm is normally a symlink to npm-cli.js.
        cli = node.parent / 'node_modules/npm/bin/npm-cli.js' if F.IS_WIN else npm.resolve()
        check = subprocess.run([str(node), '-e',
            'const s=require(require.resolve("semver",{paths:[process.argv[1]]}));'
            'console.log(process.version);process.exit(s.satisfies(process.version,process.argv[2])?0:1)',
            str(cli.parent), required], cwd=web, capture_output=True, text=True)
        if check.returncode:
            failures.append(f'{node}: {check.stdout.strip()} {check.stderr.strip()}')
            continue
        os.environ['PATH'] = str(node.parent) + os.pathsep + os.environ.get('PATH', '')
        F.log(f'Node.js: {check.stdout.strip()} ({node})，要求 {required}')
        F.run([npm, '--version'], web)
        return str(npm)
    F.err(f'WebUI 需要 Node.js {required} 和配套 npm；请安装兼容版本，或在 .env 设置 node_path。\n'
          + '\n'.join(failures))


def prepare_android_sdk(args, repo):
    cfg = F.load_config()
    local = repo / 'local.properties'
    local_sdk = ''
    if local.is_file():
        match = re.search(r'^\s*sdk\.dir\s*[=:]\s*(.*?)\s*$', local.read_text(), re.M)
        if match:
            local_sdk = re.sub(r'\\(.)', r'\1', match[1])
    explicit = str(args.android_sdk) if args.android_sdk else F.cget(cfg, 'android_home', 'android_sdk_root')
    bundled_sdk, bundled_jdk = toolchains.android_paths(cfg)
    selected = explicit or local_sdk or (str(bundled_sdk) if bundled_sdk.is_dir() else '')
    if F.DRY_RUN:
        F.log(f'(dry-run) 检查 Android SDK/JDK: {selected or "ANDROID_HOME 或 local.properties sdk.dir"}')
        return
    if not selected:
        F.err('未指定 Android SDK；请先 fetch android-sdk，或设置 --android-sdk、ANDROID_HOME、local.properties 的 sdk.dir')
    sdk = Path(selected).expanduser().resolve()
    if local_sdk:
        local_path = Path(local_sdk).expanduser()
        local_path = (local_path if local_path.is_absolute() else repo / local_path).resolve()
        if explicit and local_path != sdk:
            F.err(f'local.properties 的 sdk.dir 与指定 SDK 不一致，请先统一路径: {local_path} / {sdk}')
        if not explicit:
            sdk = local_path
    if not any((sdk / 'platforms').glob('android-*/android.jar')) or not any((sdk / 'build-tools').glob('*/aapt2')):
        F.err(f'Android SDK 不完整: {sdk}；请安装项目 compileSdk 对应的 platforms 和 build-tools')
    java_home = F.cget(cfg, 'java_home')
    if not java_home and (bundled_jdk / 'bin/javac').is_file():
        java_home = str(bundled_jdk)
    java = str(Path(java_home).expanduser() / 'bin/java') if java_home else 'java'
    javac = str(Path(java_home).expanduser() / 'bin/javac') if java_home else 'javac'
    if not shutil.which(java) or not shutil.which(javac):
        F.err('未找到完整 JDK；请安装项目要求的 JDK 并设置 JAVA_HOME（需要 java 和 javac）')
    result = subprocess.run([java, '-version'], capture_output=True, text=True)
    if result.returncode:
        F.err('JDK 无法运行: ' + result.stderr.strip())
    if java_home:
        os.environ['JAVA_HOME'] = str(Path(java_home).expanduser().resolve())
    os.environ['ANDROID_HOME'] = os.environ['ANDROID_SDK_ROOT'] = str(sdk)
    F.log(f'Android SDK: {sdk}')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def copy_payload_tree(source, destination, target_os):
    # On macOS, signatures of managed DLLs live in extended attributes.
    # Python shutil on Apple's Python does not preserve them; ditto also
    # preserves bundle symlinks and resource forks.
    if target_os == 'mac':
        F.run(['ditto', source, destination])
    else:
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def delivery(root, args, target_os, arch, version):
    if args.delivery:
        path = args.delivery.expanduser().resolve()
    else:
        pattern = re.compile(re.escape(f'arupa-{target_os}-{arch}-{version}-static-') + r'(\d+)$')
        choices = [(int(m[1]), p) for p in (root / 'dist').glob('*')
                   if p.is_dir() and (m := pattern.fullmatch(p.name))]
        if not choices:
            F.err('没有匹配的桌面内核交付包；先构建 arupa_desktop，或使用 --delivery 指定')
        path = max(choices, key=lambda pair: pair[0])[1]
    library = {'win': 'arupa_kernel.dll', 'mac': 'libarupa_kernel.dylib', 'linux': 'libarupa_kernel.so'}[target_os]
    mark = path / 'kernel/.arupa-version'
    if not (path / 'kernel' / library).is_file() or not mark.is_file():
        F.err(f'内核交付包不完整: {path}')
    if mark.read_text().strip() != version:
        F.err(f'内核交付版本与 --ver 不一致: {path}')
    return path


def pc_project(repo, target_os, explicit):
    if explicit:
        path = explicit.expanduser().resolve()
    else:
        folder = {'win': 'NomadBrowser.Avalonia', 'mac': 'NomadBrowser.Avalonia.Mac',
                  'linux': 'NomadBrowser.Avalonia.Linux'}[target_os]
        path = repo / folder / (folder + '.csproj')
    if not path.is_file():
        F.err(f'缺少浏览器项目 {path}；请先 fetch nomadbrowser.pc，或使用 --pc-project 指定实际工程')
    return path


def pc_build(root, args, target_os, arch, version, output):
    repo = root / 'nomadbrowser.pc'
    project = pc_project(repo, target_os, args.pc_project)
    nuget_config = (args.nuget_config or Path(__file__).resolve().parent.parent / 'build/nuget.config').expanduser().resolve()
    if not nuget_config.is_file():
        F.err(f'NuGet 配置不存在: {nuget_config}')
    try:
        ET.parse(nuget_config)
    except ET.ParseError as exc:
        F.err(f'NuGet 配置 XML 无效: {nuget_config}: {exc}')
    restore_props = [f'-p:RestoreConfigFile={nuget_config}']
    F.log(f'NuGet 配置: {nuget_config}')
    kernel = delivery(root, args, target_os, arch, version)
    rid = f'{"osx" if target_os == "mac" else target_os}-{arch}'
    if target_os == 'linux' and arch == 'x86':
        F.log('Linux x86 发布要求项目提供 linux-x86 运行时及原生依赖；官方 .NET SDK 无法保证支持')
    cfg = args.variant.capitalize()
    props = [f'-p:ArupaDeliveryRoot={kernel}', f'-p:ArupaSdkDir={kernel}',
             f'-p:Platform={arch}', '-p:UseSharedCompilation=false', *restore_props]
    if target_os == 'mac':
        props += ['-p:BuildMac=true', f'-p:MacRuntimeIdentifier={rid}']
        developer_dir = F.cget(F.load_config(), 'pc_developer_dir')
        if developer_dir:
            os.environ['DEVELOPER_DIR'] = developer_dir
    dotnet = prepare_dotnet(args, repo, project)
    if target_os == 'mac':
        # Run the browser's authoritative delivery contract before WebUI/publish.
        F.run([dotnet, 'msbuild', project, '-t:ValidateArupaDelivery',
               f'-p:RuntimeIdentifier={rid}', *props], repo)
    web = repo / 'NomadWebUI/nomadwebui'
    if not args.no_web:
        if not (web / 'package.json').is_file():
            F.err(f'缺少 WebUI 项目: {web}；已有资源时可显式 --no-web')
        npm = prepare_web_tools(web)
        if not (web / 'node_modules').is_dir():
            F.run([npm, 'ci' if (web / 'package-lock.json').exists() else 'install'], web)
        F.run([npm, 'run', 'build'], web)
    facade = kernel / 'dotnet/ArupaKernel.csproj'
    # A bundled DLL may predate the source (and lack OpenDevTools). Rebuild
    # incrementally into the exact directory consumed by the Windows host.
    if target_os != 'mac' and facade.is_file():
        F.run([dotnet, 'build', facade, '-c', cfg, f'-p:Platform={arch}', '-o', kernel / 'dotnet', *restore_props], repo)
    stage = output.parent / ('.' + output.name + '.publish') if F.DRY_RUN else Path(tempfile.mkdtemp(prefix='.publish-', dir=output.parent))
    try:
        bundle_root = stage / 'bundles'
        publish_props = props + ([f'-p:MacDistRoot={bundle_root}', f'-p:LocalDebugOutputRoot={stage / "local"}'] if target_os == 'mac' else [])
        if target_os == 'linux':
            publish_props += ['-f', 'net10.0', '-p:EnableWindowsTargeting=true']
        F.run([dotnet, 'publish', project, '-c', cfg, '-r', rid, '--self-contained', 'true',
               '-o', stage / 'payload', *publish_props], repo)
        if target_os == 'win':
            for folder, subdir in (('NomadBrowser.Updater', ''), ('NomadBrowser.Windows.Updater', 'Helpers/WindowsUpdater')):
                updater = repo / folder / (folder + '.csproj')
                if not updater.is_file():
                    F.err(f'缺少更新器工程: {updater}')
                F.run([dotnet, 'publish', updater, '-c', cfg, '-r', rid, '--self-contained', 'true',
                       '-p:PublishAot=false', '-o', stage / 'payload' / subdir, *props], repo)
        if F.DRY_RUN:
            return
        if target_os == 'mac':
            apps = list(bundle_root.glob('*.app')) + list(bundle_root.glob('*/*.app'))
            if len(apps) != 1:
                F.err(f'期望 MacDistRoot 下唯一 .app，实际 {len(apps)} 个: {bundle_root}')
            payload = stage / 'delivery'
            payload.mkdir()
            copy_payload_tree(apps[0], payload / apps[0].name, target_os)
            F.run(['codesign', '--verify', '--deep', '--strict', payload / apps[0].name])
            if not (payload / apps[0].name / 'Contents/Resources/arupa-mac').is_dir():
                F.err('macOS bundle 缺少内核 Contents/Resources/arupa-mac')
        else:
            payload = stage / 'payload'
            executable = payload / ('NomadBrowser.exe' if target_os == 'win' else 'NomadBrowser')
            if not executable.is_file():
                F.err(f'发布缺少浏览器可执行文件: {executable}')
            shutil.copytree(kernel / 'kernel', payload / 'arupa-desktop', dirs_exist_ok=True, symlinks=True)
            resources = [repo / 'dist' / name / 'Resources' for name in (cfg, 'Debug', 'Release')]
            resource = next((p for p in resources if (p / 'index.html').is_file()), None)
            if resource:
                shutil.copytree(resource, payload / 'Resources', dirs_exist_ok=True)
            if not (payload / 'Resources/index.html').is_file():
                F.err('发布缺少 WebUI Resources/index.html')
            if target_os == 'linux':
                launcher = payload / 'run.sh'
                launcher.write_text(
                    '#!/usr/bin/env bash\nset -e\n'
                    'BROWSER_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"\n'
                    'cd "$BROWSER_DIR"\n'
                    'export LD_LIBRARY_PATH="$BROWSER_DIR/arupa-desktop${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
                    'exec "$BROWSER_DIR/NomadBrowser" "$@"\n', encoding='utf-8')
                launcher.chmod(0o755)
        save_output(payload, output)
    finally:
        if not F.DRY_RUN:
            shutil.rmtree(stage, ignore_errors=True)


def validate_apk(path, arch):
    abi = {'arm64': 'arm64-v8a', 'x64': 'x86_64'}[arch]
    with zipfile.ZipFile(path) as archive:
        abis = {name.split('/')[1] for name in archive.namelist()
                if name.startswith('lib/') and name.endswith('.so') and len(name.split('/')) > 2}
    if abis != {abi}:
        F.err(f'APK 架构不匹配: {path}，期望 {abi}，实际 {sorted(abis)}')


def android_build(root, args, arch, output):
    repo = root / 'nomadbrowser.android'
    wrapper = repo / 'gradlew'
    if not wrapper.is_file():
        F.err(f'缺少 {wrapper}；请先 fetch nomadbrowser.android')
    prepare_android_sdk(args, repo)
    aar = repo / f'app/libs/kernel/{arch}/arupa-kernel.aar'
    if not aar.is_file():
        F.err(f'缺少内核 AAR: {aar}；请先将 arupa_android 交付件接入浏览器')
    with zipfile.ZipFile(aar) as archive:
        abi = {'arm64': 'arm64-v8a', 'x64': 'x86_64'}[arch]
        if f'jni/{abi}/libarupakernel.so' not in archive.namelist():
            F.err(f'AAR 缺少 {abi} 内核: {aar}')
    manifest = repo / 'tools/ci/runtime-manifest.json'
    if manifest.exists():
        expected = json.loads(manifest.read_text()).get('files', {}).get(aar.relative_to(repo).as_posix())
        if expected and expected != digest(aar):
            F.err('AAR 与 runtime-manifest.json 指纹不匹配；请同步完整内核交付，不能混用版本')
    # Gradle's ABI builds share outputs. Clean before each ABI so stale APKs
    # cannot be mislabeled as the next architecture.
    F.run(['bash', wrapper, ':app:clean', f':app:assemble{args.variant.capitalize()}',
           f'-PkernelAbi={arch}', '--max-workers', str(args.jobs)], repo)
    if F.DRY_RUN:
        return
    apk_dir = repo / 'app/build/outputs/apk' / args.variant
    apks = sorted(apk_dir.glob('*.apk'))
    if not apks:
        F.err(f'Gradle 未产出 APK: {apk_dir}')
    for apk in apks:
        validate_apk(apk, arch)
    with tempfile.TemporaryDirectory(prefix='.apk-', dir=output.parent) as temporary:
        payload = Path(temporary) / 'payload'
        payload.mkdir()
        for apk in apks:
            shutil.copy2(apk, payload / apk.name)
        save_output(payload, output)


def save_output(payload, output):
    # Only replace previously generated output after a successful build.
    if output.exists():
        shutil.rmtree(output)
    shutil.move(str(payload), output)


def seal(output, identity):
    files = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
    (output / 'build-manifest.json').write_text(json.dumps({'identity': identity, 'files': files}, indent=2) + '\n')


def package(args, output, identity):
    if F.DRY_RUN:
        F.log(f'(dry-run) 校验并打包 {output} -> {args.dist_dir}')
        return
    stamp = output / 'build-manifest.json'
    if not stamp.is_file():
        F.err(f'缺少成功构建清单: {stamp}；请先 build')
    manifest = json.loads(stamp.read_text())
    if manifest['identity'] != identity:
        F.err(f'构建配置与打包请求不符: {output}')
    actual = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*')
              if p.is_file() and p != stamp}
    if actual != manifest['files']:
        F.err(f'构建产物在 build 后发生变化，请重新 build: {output}')
    destination = args.dist_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    prefix = output.name + '-'
    pattern = re.compile(re.escape(prefix) + r'(\d+)$')
    number = args.num or max([int(m[1]) for p in destination.iterdir() if (m := pattern.fullmatch(p.name))] + [0]) + 1
    final = destination / f'{prefix}{number}'
    if final.exists() or Path(str(final) + '.zip').exists():
        F.err(f'交付包已存在: {final}')
    lock = destination / (final.name + '.lock')
    with lock.open('x'):
        pass
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix='.browser-package-', dir=destination))
        copy_payload_tree(output, stage, identity['os'])
        if identity['os'] == 'mac':
            for app in stage.glob('*.app'):
                F.run(['codesign', '--verify', '--deep', '--strict', app])
        (stage / 'SHA256SUMS.txt').write_text(''.join(f'{hash_value}  {name}\n' for name, hash_value in sorted(actual.items())))
        stage.rename(final)
        if args.zip:
            archive_path = Path(str(final) + '.zip')
            try:
                if identity['os'] == 'mac':
                    F.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', final, archive_path])
                else:
                    write_portable_zip(final, archive_path, destination)
            except BaseException:
                archive_path.unlink(missing_ok=True)
                raise
    finally:
        if stage and stage.exists():
            shutil.rmtree(stage)
        lock.unlink()
    F.log(f'浏览器交付包: {final}')


def write_portable_zip(final, archive_path, destination):
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(final.rglob('*')):
            name = path.relative_to(destination).as_posix()
            if path.is_symlink():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
                archive.writestr(entry, os.readlink(path))
            else:
                archive.write(path, name)


def run(root, args, project, target_os, arches, version, actions):
    if 'gen' in actions:
        F.err('浏览器使用 .NET/Gradle；不支持 gen。GN 配置请用于 arupa_desktop/arupa_android')
    if args.args:
        F.err('--args 仅用于 Arupa 内核；浏览器通过交付件消费内核')
    if project == 'nomadbrowser.android' and args.delivery:
        F.err('Android 请将内核交付接入 app/libs/kernel/<arch> 并更新 runtime-manifest.json；--delivery 用于 PC')
    for arch in arches:
        identity = {'project': project, 'os': target_os, 'arch': arch, 'version': version, 'variant': args.variant}
        output = root / 'out' / f'{project}-{target_os}-{arch}-{version}-{args.variant}'
        if 'build' in actions:
            if not F.DRY_RUN:
                output.parent.mkdir(parents=True, exist_ok=True)
                # A failed rebuild must not permit packaging yesterday's output.
                (output / 'build-manifest.json').unlink(missing_ok=True)
            if project == 'nomadbrowser.pc':
                pc_build(root, args, target_os, arch, version, output)
            else:
                android_build(root, args, arch, output)
            if not F.DRY_RUN:
                seal(output, identity)
        if 'package' in actions:
            package(args, output, identity)
