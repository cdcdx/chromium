#!/usr/bin/env python3
"""MiVPN ZIP 签名工具：直接签名、验签及管理发布公钥。"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'arupa_desktop/plugin/mivpn_resources'
sys.path.insert(0, str(BACKEND))
import mivpn_package as package

DEFAULT_PUBLIC_KEY = BACKEND / 'mivpn-release-20261103.pub.pem'
ZIP_LIMIT = 128 << 20


class ToolError(ValueError):
    pass


def location(value):
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    # Inspect the original components first: normalizing a symlink/.. pair
    # before this check could hide the link. abspath then removes lexical dot
    # components without following links, so repository containment is sound.
    package.checked_path(raw)
    return Path(os.path.abspath(raw))


def checked_file(path):
    package.checked_path(path)
    if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
        raise ToolError('文件不存在或不是普通文件；请检查输入 ZIP / 密钥路径。')


def checked_destination(path):
    package.checked_path(path)
    if not path.parent.is_dir():
        raise ToolError('输出目录不存在；请先创建目录。')
    if path.exists():
        raise ToolError('输出文件已存在，未覆盖。请选择新的 --output；原位签名须显式使用 --in-place。')


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def capture_zip(path):
    checked_file(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > ZIP_LIMIT:
            raise ToolError('输入必须是普通 ZIP 文件，压缩包大小不得超过 128 MiB。')
        raw = source.read(ZIP_LIMIT + 1)
        if (len(raw) > ZIP_LIMIT or len(raw) != info.st_size
                or fingerprint(info) != fingerprint(os.fstat(source.fileno()))):
            raise ToolError('读取期间输入 ZIP 发生变化；未覆盖原包。')
    return raw, fingerprint(info)


def require_unchanged_zip(path, raw, identity, message):
    # Use the same handle-based API for both snapshots. On Windows/Python 3.12,
    # Path.stat().st_ctime_ns can describe creation time while os.fstat() exposes
    # change time after an atomic replacement. Comparing those APIs falsely
    # rejects an unchanged re-signed ZIP. Reopening also rechecks the path and
    # retains every identity/timestamp field plus the complete byte comparison.
    current, current_identity = capture_zip(path)
    if current_identity != identity or current != raw:
        raise ToolError(message)


def stage_file(destination, raw, mode=0o644):
    package.checked_path(destination)
    fd, name = tempfile.mkstemp(prefix='.mivpn-publish-', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def publish_new(destination, raw, mode=0o644):
    checked_destination(destination)
    temporary = stage_file(destination, raw, mode)
    try:
        # An atomic no-clobber publish on macOS/Linux/NTFS. A filesystem without
        # hard-link support fails safely instead of overwriting an existing file.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def signing_key(value):
    value = value or os.environ.get('MIVPN_SIGNING_KEY_FILE')
    if not value:
        raise ToolError('缺少发布私钥：请提供 --key，或设置 MIVPN_SIGNING_KEY_FILE。')
    path = location(value)
    checked_file(path)
    return path


def public_metadata(public):
    return {'keyId': package.key_id(public), 'publicKeyHex': public.hex(),
            'gnArg': f'arupa_mivpn_signing_public_keys = [ "{public.hex()}" ]'}


def sign(args):
    source = location(args.input)
    if args.in_place and args.output:
        raise ToolError('--in-place 与 --output 不能同时使用。')
    destination = source if args.in_place else location(
        args.output or source.with_name(source.stem + '-signed.zip'))
    if not args.in_place:
        if destination == source:
            raise ToolError('覆盖输入 ZIP 须显式使用 --in-place。')
        checked_destination(destination)
    private = signing_key(args.key)
    raw, identity = capture_zip(source)
    resources = package.read_zip(io.BytesIO(raw))
    # Re-signing deliberately replaces only the two defined envelope files.
    for name in package.METADATA:
        resources.pop(name, None)
    if any(name.startswith('META-INF/') for name in resources):
        raise ToolError('META-INF 只能包含 mivpn-package.json 和 mivpn-package.sig。')
    if any(b'-----BEGIN ' in body and b'PRIVATE KEY-----' in body for body in resources.values()):
        raise ToolError('ZIP 中发现私钥内容；拒绝将私钥打入插件包。')
    package.validate_resources(resources)
    binary = package.openssl_binary(args.openssl)
    try:
        package.public_key_bytes(private, binary, private=True)
    except package.PackageError as error:
        raise ToolError('发布私钥无效或不是 Ed25519 私钥；未修改原 ZIP。') from error
    with tempfile.TemporaryDirectory(prefix='mivpn-zip-sign-') as directory:
        temporary = Path(directory).resolve()
        temporary.chmod(0o700)
        tree = temporary / 'resources'
        tree.mkdir(mode=0o700)
        for name, body in resources.items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open('xb') as output:
                output.write(body)
            target.chmod(0o644)
        signed = temporary / 'signed.zip'
        result = package.sign_package(tree, private, signed,
                                      min_kernel_abi_minor=args.min_kernel_abi_minor,
                                      openssl=binary)
        signed_bytes = signed.read_bytes()
    backup = None
    if args.in_place:
        require_unchanged_zip(source, raw, identity, '签名期间输入 ZIP 发生变化；未覆盖原包。')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = source.with_name(source.stem + '.backup-' + stamp + '-' + uuid.uuid4().hex + '.zip')
        # Prepare every byte before replacing the original. Backups are unique,
        # published exclusively, and never overwritten by a later signing run.
        staged = stage_file(source, signed_bytes)
        try:
            publish_new(backup, raw, mode=0o600)
            require_unchanged_zip(source, raw, identity, '发布前输入 ZIP 发生变化；未覆盖原包，唯一备份已保留。')
            os.replace(staged, source)
        finally:
            staged.unlink(missing_ok=True)
    else:
        publish_new(destination, signed_bytes)
    return {**result, 'input': str(source), 'output': str(destination),
            'backup': str(backup) if backup else None,
            'sha256': hashlib.sha256(signed_bytes).hexdigest()}


def verify(args):
    source, public = location(args.input), location(args.public_key)
    checked_file(public)
    raw, _ = capture_zip(source)
    binary = package.openssl_binary(args.openssl)
    result = package.verify_resources(package.read_zip(io.BytesIO(raw)),
                                      package.public_key_bytes(public, binary), binary)
    return {**result, 'input': str(source), 'sha256': hashlib.sha256(raw).hexdigest()}


def export_public(args):
    binary = package.openssl_binary(args.openssl)
    private = not args.public_key
    path = signing_key(args.key) if private else location(args.public_key)
    checked_file(path)
    public = package.public_key_bytes(path, binary, private=private)
    result = public_metadata(public)
    if args.output:
        arguments = ['pkey', '-in', path, '-pubout']
        if not private:
            arguments.insert(1, '-pubin')
        target = location(args.output)
        publish_new(target, package.openssl_run(binary, arguments))
        result['publicKeyFile'] = str(target)
    return result


def keygen(args):
    private = location(args.key)
    public = location(args.public_key or private.with_suffix('.pub.pem'))
    if private == public or ROOT == private or ROOT in private.parents:
        raise ToolError('新私钥必须保存在仓库外，且私钥和公钥输出路径必须不同。')
    checked_destination(private)
    checked_destination(public)
    binary = package.openssl_binary(args.openssl)
    print('提示：keygen 会创建全新发布者。现有 MiVPN 发布请继续使用原私钥；'
          '新公钥必须配置进三端内核后才能安装。', file=sys.stderr)
    # Retain the existing exclusive O_EXCL/0600 private-key creation contract.
    completed = subprocess.run([sys.executable, str(BACKEND / 'mivpn_signing_key.py'),
        'generate', '--private-key-file', str(private), '--public-key-file', str(public),
        '--openssl', str(binary)], capture_output=True, check=False)
    if completed.returncode:
        raise ToolError('密钥生成失败；不会覆盖已有密钥，请检查输出路径和权限。')
    return {**json.loads(completed.stdout), 'publicKeyFile': str(public)}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    signing = sub.add_parser('sign', help='直接为 ZIP 生成 Ed25519 签名')
    signing.add_argument('--input', required=True, help='输入 ZIP，根目录必须包含 manifest.json')
    signing.add_argument('--output', help='新输出 ZIP；默认在输入文件名后加 -signed')
    signing.add_argument('--key', help='发布私钥 PEM；也可使用 MIVPN_SIGNING_KEY_FILE')
    signing.add_argument('--in-place', action='store_true', help='原位原子替换，自动保留唯一备份')
    signing.add_argument('--min-kernel-abi-minor', type=int, default=57)
    signing.set_defaults(handler=sign)
    verification = sub.add_parser('verify', help='使用发布公钥验证 ZIP')
    verification.add_argument('--input', required=True)
    verification.add_argument('--public-key', default=str(DEFAULT_PUBLIC_KEY),
                              help='公钥 PEM；默认使用仓库内当前发布公钥')
    verification.set_defaults(handler=verify)
    public = sub.add_parser('public-key', help='导出公钥信息，不输出私钥')
    inputs = public.add_mutually_exclusive_group()
    inputs.add_argument('--key', help='从发布私钥导出；也可使用 MIVPN_SIGNING_KEY_FILE')
    inputs.add_argument('--public-key', help='读取已有公钥 PEM')
    public.add_argument('--output', help='可选的新公钥 PEM 输出；不覆盖已有文件')
    public.set_defaults(handler=export_public)
    generation = sub.add_parser('keygen', help='仅首次建立发布者时生成仓库外的新密钥')
    generation.add_argument('--key', required=True, help='新私钥输出路径，必须位于仓库外')
    generation.add_argument('--public-key', help='新公钥输出，默认 <key>.pub.pem')
    generation.set_defaults(handler=keygen)
    for command in (signing, verification, public, generation):
        command.add_argument('--openssl', help='显式 OpenSSL 3+ 路径；默认自动检测')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ToolError as error:
        parser.exit(1, f'MiVPN 操作失败：{error}\n')
    except package.PackageError as error:
        parser.exit(1, 'MiVPN 校验失败：' + str(error) +
                    '。请检查 ZIP 结构、manifest 和发布密钥；原输入包未覆盖。\n')
    except (zipfile.BadZipFile, NotImplementedError, EOFError, RuntimeError):
        parser.exit(1, 'MiVPN 操作失败：ZIP 损坏、加密或压缩格式不受支持；原输入包未覆盖。\n')
    except OSError:
        parser.exit(1, 'MiVPN 文件操作失败：请检查路径、权限与磁盘；未覆盖已有输出。\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
