#!/usr/bin/env python3
"""MiVPN 目录/ZIP -> 完整签名 ZIP 或 CRX3（Python 3.9+、OpenSSL 3+）。

在仓库根目录运行：
  python3 scripts/pack_mivpn.py --format both
  python3 scripts/pack_mivpn.py --source dist/hcjdpccaikglmijenhghaddadbmmhgea --format zip
  python3 scripts/pack_mivpn.py --format crx --key dist/mivpn-extension-0.0.27-20261107.pem
  python3 scripts/pack_mivpn.py --format both --package-key /secure/mivpn-release.pem

默认输出到 dist/mivpn-packaged/，不覆盖输入。也可以将本脚本单独复制给其他人，
显式传入 --source、--key、--package-key 和 --output-dir。
--key 是外层 CRX 的 RSA 私钥；--package-key 是内层 MiVPN 的 Ed25519 发布私钥。
内层私钥依次从 --package-key、MIVPN_SIGNING_KEY_FILE、当前用户的
~/.local/share/arupa/mivpn-signing/mivpn-release-20261103.pem 读取。
找到发布私钥时，自动枚举所有资源（包括所有 engine/**），重建清单并签名、验签。
找不到发布私钥时，仅允许重新封装已经通过完整验签的包；清单缺失/过期会报错。
--keep-package-signature 可强制只验签、不重新签名。ZIP 无需 CRX RSA 私钥。
本工具不修改业务文件、版本或扩展 ID，不生成新私钥。
"""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

EXTENSION_ID = 'hcjdpccaikglmijenhghaddadbmmhgea'
DIST = Path(__file__).resolve().parent.parent / 'dist'
LIMIT = 512 << 20
CONTEXT = b'CRX3 SignedData\x00'
INVENTORY = 'META-INF/mivpn-package.json'
PACKAGE_SIGNATURE = 'META-INF/mivpn-package.sig'
PACKAGE_METADATA = {INVENTORY, PACKAGE_SIGNATURE}
PACKAGE_DOMAIN = b'ARUPA-MIVPN-PACKAGE\0v1\0'
# 当前 MiVPN 发布公钥（不是私钥）；与 tools/signing/mivpn-release-20261103.pub.pem 一致。
PACKAGE_PUBLIC_DER = bytes.fromhex(
    '302a300506032b6570032100ee9860012d3dc7234d2b1b6ec291daf889f570477f7595807e9dc3000ee0a886')
DEFAULT_PACKAGE_KEY = Path.home() / '.local/share/arupa/mivpn-signing/mivpn-release-20261103.pem'


def extension_id(public):
    return ''.join(chr(97 + int(c, 16)) for c in hashlib.sha256(public).hexdigest()[:32])


def check_name(name):
    if (not name or '\\' in name or ':' in name or any(ord(c) < 32 for c in name)
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError(f'包内路径不安全：{name!r}')


def ignored(name):
    parts = name.split('/')
    return (any(p in ('.DS_Store', '__MACOSX', '.git', '__pycache__')
                or p.startswith('._') for p in parts)
            or any(p.lower().startswith('.nomad-local-install.json') for p in parts))


def read_source(source):
    files, seen, skipped = {}, set(), []
    total = 0

    def add(name, body, mode):
        nonlocal total
        check_name(name)
        if name.casefold() in seen:
            raise ValueError(f'包内存在重复或大小写冲突路径：{name}')
        seen.add(name.casefold())
        if ignored(name):
            skipped.append(name)
            return
        if b'-----BEGIN ' in body and b'PRIVATE KEY-----' in body:
            raise ValueError(f'输入目录/ZIP 含私钥，拒绝打入包：{name}')
        total += len(body)
        if total > LIMIT or len(files) >= 4096:
            raise ValueError('包展开后不得超过 512 MiB / 4096 个文件')
        files[name] = (body, 0o755 if mode & 0o111 else 0o644)

    if source.is_dir():
        for path in sorted(source.rglob('*')):
            name = path.relative_to(source).as_posix()
            if ignored(name):
                if path.is_file():
                    skipped.append(name)
                continue
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f'不支持符号链接或特殊文件：{name}')
            if path.stat().st_size > LIMIT - total:
                raise ValueError('包展开后不得超过 512 MiB')
            add(name, path.read_bytes(), mode)
    else:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 8192:
                raise ValueError('ZIP 条目过多')
            names = [e.filename for e in entries if not e.is_dir() and not ignored(e.filename)]
            if 'manifest.json' in names:
                prefix = ''
            else:
                roots = {n.split('/')[0] for n in names}
                if len(roots) != 1:
                    raise ValueError('ZIP 根目录或唯一的顶层目录必须包含 manifest.json')
                prefix = next(iter(roots)) + '/'
                if prefix + 'manifest.json' not in names:
                    raise ValueError('找不到 manifest.json；请指定插件根目录或 ZIP')
            for entry in entries:
                check_name(entry.filename.rstrip('/'))
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f'ZIP 中存在符号链接：{entry.filename}')
                if entry.is_dir():
                    continue
                if ignored(entry.filename):
                    skipped.append(entry.filename)
                    continue
                if not entry.filename.startswith(prefix):
                    raise ValueError(f'ZIP 存在插件目录之外的文件：{entry.filename}')
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError(f'ZIP 中存在特殊文件：{entry.filename}')
                if entry.file_size > LIMIT - total:
                    raise ValueError('包展开后不得超过 512 MiB')
                add(entry.filename[len(prefix):], archive.read(entry), mode)
    if 'manifest.json' not in files:
        raise ValueError('输入目录必须直接包含 manifest.json')
    for name in files:
        parts = name.split('/')
        if any('/'.join(parts[:i]).casefold() in seen for i in range(1, len(parts))):
            raise ValueError(f'文件与目录冲突：{name}')
    return files, skipped


def update_package_metadata(files, manifest, key, binary, min_abi=None):
    """重建全量清单；没有私钥时必须完整验证原清单和签名，禁止带警告出包。"""
    resources = {name: body for name, (body, _) in files.items() if name not in PACKAGE_METADATA}
    for name, body in resources.items():
        check_name(name)
        if name.startswith('META-INF/'):
            raise ValueError(f'META-INF 含不支持的文件：{name}')
        if len(body) > (256 << 20 if name.startswith('engine/') else 8 << 20):
            raise ValueError(f'文件超出 MiVPN 签名包大小限制：{name}')
        # 与内核的跨平台路径约束一致，避免签出另一端不能暂存的资源。
        for part in name.split('/'):
            stem = part.split('.')[0].upper()
            if (part[-1] in '. ' or any(ord(c) < 32 or ord(c) > 126 or c in '<>:"\\|?*' for c in part)
                    or stem in {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$'}
                    or re.fullmatch(r'(COM|LPT)[1-9]', stem)):
                raise ValueError(f'不支持的跨平台资源路径：{name}')
    version = manifest.get('version')
    if (manifest.get('manifest_version') != 3 or not isinstance(version, str)
            or not re.fullmatch(r'(0|[1-9][0-9]{0,4})(\.(0|[1-9][0-9]{0,4})){0,3}', version)
            or any(int(n) > 65535 for n in version.split('.'))
            or not any(int(n) for n in version.split('.'))):
        raise ValueError('manifest 必须是 MV3 且包含有效版本号')
    previous = None
    if INVENTORY in files:
        try:
            previous = json.loads(files[INVENTORY][0])
        except (ValueError, UnicodeError) as error:
            raise ValueError('已有 MiVPN 清单不可解析') from error
        if not isinstance(previous, dict):
            raise ValueError('已有 MiVPN 清单必须是 JSON 对象')
    if min_abi is None:
        min_abi = previous.get('minKernelAbiMinor') if previous is not None else 57
    if type(min_abi) is not int or not 0 <= min_abi <= 65535:
        raise ValueError('minKernelAbiMinor 必须在 0..65535 范围内')
    key_id = hashlib.sha256(PACKAGE_PUBLIC_DER[12:]).hexdigest()[:32]
    expected = {'schema': 1, 'product': 'mivpn', 'extensionId': EXTENSION_ID,
                'version': version, 'keyId': key_id, 'minKernelAbiMinor': min_abi,
                'files': [{'path': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                          for name, body in sorted(resources.items())]}
    if key is None:
        if previous != expected or PACKAGE_SIGNATURE not in files:
            raise ValueError('MiVPN 签名清单缺失或与资源不一致；请通过 --package-key 指定 '
                             'Ed25519 发布私钥以重建完整清单，不能使用 CRX 的 RSA PEM 代替')
        inventory, signature = files[INVENTORY][0], files[PACKAGE_SIGNATURE][0]
    else:
        public = openssl(binary, 'pkey', '-in', key, '-passin', 'pass:', '-pubout', '-outform', 'DER')
        if public != PACKAGE_PUBLIC_DER:
            raise ValueError('--package-key 必须是当前受信的 Ed25519 发布私钥；不能使用 CRX RSA 私钥或其他密钥')
        inventory = (json.dumps(expected, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()
    if len(inventory) > 1 << 20:
        raise ValueError('MiVPN 签名清单超过 1 MiB')
    with tempfile.TemporaryDirectory(prefix='mivpn-package-sign-') as directory:
        directory = Path(directory)
        message, sigfile, pubfile = (directory / name for name in ('message', 'signature', 'public.der'))
        message.write_bytes(PACKAGE_DOMAIN + inventory)
        pubfile.write_bytes(PACKAGE_PUBLIC_DER)
        if key is not None:
            signature = openssl(binary, 'pkeyutl', '-sign', '-rawin', '-inkey', key,
                                '-passin', 'pass:', '-in', message)
        if len(signature) != 64:
            raise ValueError('MiVPN Ed25519 签名长度错误')
        sigfile.write_bytes(signature)
        try:
            openssl(binary, 'pkeyutl', '-verify', '-rawin', '-pubin', '-keyform', 'DER',
                    '-inkey', pubfile, '-in', message, '-sigfile', sigfile)
        except ValueError as error:
            raise ValueError('MiVPN 内层签名验证失败；未输出新包') from error
    files[INVENTORY] = (inventory, 0o644)
    files[PACKAGE_SIGNATURE] = (signature, 0o644)
    return {'action': 'regenerated' if key is not None else 'verified', 'keyId': key_id,
            'signedResourceCount': len(resources), 'minKernelAbiMinor': min_abi,
            'engineFiles': [name for name in sorted(resources) if name.startswith('engine/')]}


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, (body, mode) in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, body)
    blob = stream.getvalue()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(files):
            raise ValueError('生成的 ZIP 校验失败')
    return blob


def openssl(binary, *args):
    result = subprocess.run([binary, *map(str, args)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError('OpenSSL 操作失败；请检查密钥是否正确、未加密，以及 OpenSSL 3+ 是否可用')
    return result.stdout


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, body):
    return varint((number << 3) | 2) + varint(len(body)) + body


def crx_bytes(archive, key, binary, public):
    signed_header = field(1, hashlib.sha256(public).digest()[:16])
    message = CONTEXT + struct.pack('<I', len(signed_header)) + signed_header + archive
    with tempfile.TemporaryDirectory(prefix='mivpn-crx-') as directory:
        directory = Path(directory)
        payload, sigfile, pubfile = (directory / n for n in ('payload.bin', 'signature.bin', 'public.pem'))
        payload.write_bytes(message)
        signature = openssl(binary, 'dgst', '-sha256', '-sign', key, '-passin', 'pass:',
                            '-sigopt', 'rsa_padding_mode:pkcs1', payload)
        sigfile.write_bytes(signature)
        pubfile.write_bytes(openssl(binary, 'rsa', '-in', key, '-passin', 'pass:', '-pubout'))
        openssl(binary, 'dgst', '-sha256', '-verify', pubfile, '-signature', sigfile,
                '-sigopt', 'rsa_padding_mode:pkcs1', payload)
    proof = field(1, public) + field(2, signature)
    header = field(2, proof) + field(10000, signed_header)
    return b'Cr24' + struct.pack('<II', 3, len(header)) + header + archive


def publish(path, body, force):
    if path.exists() and path.read_bytes() == body:
        return
    if path.exists() and not force:
        raise ValueError(f'输出已存在且内容不同：{path}；选择新目录或使用 --force')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.pack-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(body)
        temporary.chmod(0o644)
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    default_source = DIST / EXTENSION_ID
    if not default_source.is_dir():
        default_source = default_source.with_suffix('.zip')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source', type=Path, default=default_source, help='插件根目录或 ZIP')
    parser.add_argument('--format', choices=('crx', 'zip', 'both'), default='both')
    parser.add_argument('--key', type=Path, default=DIST / 'mivpn-extension-0.0.27-20261107.pem', help='CRX RSA 私钥')
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument('--package-key', type=Path, help='MiVPN Ed25519 发布私钥，用于重建全量资源清单及签名')
    signing.add_argument('--keep-package-signature', action='store_true', help='不读取内层私钥，仅允许封装完整验签通过的包')
    parser.add_argument('--min-kernel-abi-minor', type=int, help='内层清单 ABI 要求，默认保留原值；无原清单时为 57')
    parser.add_argument('--output-dir', type=Path, default=DIST / 'mivpn-packaged')
    parser.add_argument('--openssl', default='openssl', help='OpenSSL 3+ 命令或完整路径')
    parser.add_argument('--force', action='store_true', help='允许覆盖输出文件；不会覆盖输入或私钥')
    args = parser.parse_args()
    try:
        source, key, output = (p.expanduser().resolve() for p in (args.source, args.key, args.output_dir))
        package_key = None
        if not args.keep_package_signature:
            configured_key = args.package_key or os.environ.get('MIVPN_SIGNING_KEY_FILE')
            if configured_key:
                package_key = Path(configured_key).expanduser().resolve()
            elif DEFAULT_PACKAGE_KEY.is_file():
                package_key = DEFAULT_PACKAGE_KEY.resolve()
        protected = {source, key}
        if package_key is not None:
            protected.add(package_key)
            if not package_key.is_file():
                raise ValueError('指定的 MiVPN Ed25519 发布私钥不存在')
        formats = ('zip', 'crx') if args.format == 'both' else (args.format,)
        destinations = {fmt: output / (EXTENSION_ID + '.' + fmt) for fmt in formats}
        if (source.is_dir() and (output == source or source in output.parents)
                or any(p.resolve() in protected for p in destinations.values())):
            raise ValueError('输出不能位于输入目录内，也不能覆盖输入 ZIP 或私钥')
        files, skipped = read_source(source)
        manifest = json.loads(files['manifest.json'][0])
        if not isinstance(manifest, dict) or not isinstance(manifest.get('key'), str):
            raise ValueError('manifest.json 必须含当前 MiVPN 公钥 key；脚本不修改 manifest')
        public = base64.b64decode(''.join(manifest['key'].split()), validate=True)
        if extension_id(public) != EXTENSION_ID:
            raise ValueError('manifest.key 与目标扩展 ID 不一致；未修改 manifest 或私钥')
        binary = shutil.which(args.openssl)
        if not binary:
            raise ValueError('找不到 OpenSSL 3+；请安装或使用 --openssl 指定完整路径')
        if 'crx' in formats:
            derived = openssl(binary, 'rsa', '-in', key, '-passin', 'pass:', '-pubout', '-outform', 'DER')
            if public != derived:
                raise ValueError('PEM 与 manifest.key 不匹配；继续打包会产生 crx_manifest_key_mismatch')
        package_info = update_package_metadata(files, manifest, package_key, binary, args.min_kernel_abi_minor)
        archive = zip_bytes(files)
        artifacts = {fmt: archive if fmt == 'zip' else crx_bytes(archive, key, binary, public) for fmt in formats}
        for fmt, body in artifacts.items():
            target = destinations[fmt]
            if target.exists() and not args.force and target.read_bytes() != body:
                raise ValueError(f'输出已存在且内容不同：{target}；选择新目录或使用 --force')
        outputs = []
        for fmt, body in artifacts.items():
            publish(destinations[fmt], body, args.force)
            outputs.append({'path': str(destinations[fmt]), 'bytes': len(body),
                            'sha256': hashlib.sha256(body).hexdigest()})
        print(json.dumps({'extensionId': EXTENSION_ID, 'version': manifest.get('version'),
                          'fileCount': len(files), 'excluded': skipped,
                          'packageSignature': package_info, 'warnings': [],
                          'outputs': outputs}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, f'打包失败：{error}\n')


if __name__ == '__main__':
    main()
