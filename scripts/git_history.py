#!/usr/bin/env python3
"""Git 提交记录起点增量备份 / 还原（保持 commit id hash 不变）。

固定起点模型：
  指定一个起点 commit（SHA / tag / 分支），只备份“起点之后到目标分支”的提交及其
  变更对象，输出标准 git bundle（通常 KB 级）。还原时目标仓库必须已包含该起点，
  baseline 子命令可生成只含起点快照的仓库；对象经 git fetch 原字节导入，因此
  commit / tree / blob / tag 的 hash 与原仓库完全一致。

  baseline --from <起点> -d <目录>                  准备含起点的基线仓库
  backup   --from <起点> --ref <分支> -o <bundle>   生成增量 bundle
  restore  -a <bundle> -d <仓库> [--checkout]      导入增量并校验 hash
  verify   -a <bundle> -r <仓库>                   核对引用 hash

  起点/分支名不存在时会按路径后缀唯一匹配本地引用（如 desktop/154.x 可匹配到 desktop/154.x）；
  匹配不到或匹配到多个则报错并列出候选。--base 是 --from 的兼容别名。

用法示例：
    # 1) 准备起点（SHA / tag / 分支均可）
    python3 scripts/git_history.py baseline --repo src --from 0fa6d91e2f -d patches/src-baseline

    # 2) 从固定起点备份
    python3 scripts/git_history.py backup --repo src --from 0fa6d91e2f \
        --ref desktop/154.0.8037.21 -o patches/src-inc.bundle

    # ./src 是 154.0.8037.21 分支的起点，因此备份的提交范围是 0fa6d91e2f 之后的提交。

    # 3) 还原（起点自动从 bundle 读取）
    python3 scripts/git_history.py restore -a patches/src-inc.bundle -d ./src --checkout

    # 4) 校验
    python3 scripts/git_history.py verify -a patches/src-inc.bundle -r ./src
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BUNDLE_MAGIC = (b'# v2 git bundle', b'# v3 git bundle')


class GitError(RuntimeError):
    pass


def log(message):
    print(f'[INFO] {message}', flush=True)


def warn(message):
    print(f'[WARN] {message}', file=sys.stderr, flush=True)


def human_size(size):
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024
    return f'{size} B'


def git(repo, args, *, check=True):
    """执行 git 命令，返回 CompletedProcess（bytes）。"""
    command = ['git', '--no-optional-locks', '-C', str(repo), *args]
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and process.returncode != 0:
        detail = process.stderr.decode('utf-8', 'replace').strip()
        raise GitError(f"git {' '.join(args)} 失败: {detail}")
    return process


def git_out(repo, args, *, check=True):
    return git(repo, args, check=check).stdout.decode('utf-8', 'replace').strip()


def is_repo(path):
    path = Path(path)
    return path.exists() and git(path, ['rev-parse', '--git-dir'], check=False).returncode == 0


# --------------------------------------------------------------------------- 引用解析

def local_refs(repo):
    """返回本地分支与标签的短名（增量只引用这两类可还原的引用）。"""
    out = git(repo, ['for-each-ref', '--format=%(refname)',
                     'refs/heads', 'refs/tags']).stdout
    names = []
    for line in out.decode('utf-8', 'replace').splitlines():
        name = line.strip()
        for prefix in ('refs/heads/', 'refs/tags/'):
            if name.startswith(prefix):
                names.append(name[len(prefix):])
                break
    return names


def tail_match_score(requested, candidate):
    """按路径分段从尾部逐级匹配，返回匹配层数；0 表示不匹配。"""
    want, have = requested.split('/'), candidate.split('/')
    score = 0
    for depth in range(1, min(len(want), len(have)) + 1):
        if want[-depth:] != have[-depth:]:
            break
        score = depth
    return score


def resolve_commit(repo, name, role):
    """把分支/标签/提交解析成 commit sha；名称不存在时按后缀唯一匹配并给出候选。"""
    sha = git_out(repo, ['rev-parse', '--verify', '-q', f'{name}^{{commit}}'], check=False)
    if sha:
        return name, sha

    refs = local_refs(repo)
    scored = sorted(((tail_match_score(name, ref), ref) for ref in refs), key=lambda item: -item[0])
    best = [ref for score, ref in scored if score == scored[0][0]] if scored and scored[0][0] else []
    if len(best) == 1:
        matched = best[0]
        log(f'{role} `{name}` 不存在，按引用后缀匹配到本地引用 `{matched}`')
        sha = git_out(repo, ['rev-parse', '--verify', '-q', f'{matched}^{{commit}}'], check=False)
        if sha:
            return matched, sha
    if len(best) > 1:
        raise GitError(f'无法解析{role} `{name}`：后缀同时匹配多个本地引用，请改用全名：\n'
                       + '\n'.join(f'  {ref}' for ref in best[:10]))

    hint = ''
    if refs:
        hint = '\n本地可用的分支/标签（按名称相似度排序）:\n' + '\n'.join(
            f'  {ref}' for _, ref in scored[:8])
    raise GitError(f'无法解析{role} `{name}`：本地没有这个分支、标签或提交。'
                   f'{hint}\n用 git -C <仓库> branch / git -C <仓库> tag 查看全部引用，'
                   f'或改用仓库里的全名。')


# --------------------------------------------------------------------------- bundle

def looks_like_bundle(path):
    with open(path, 'rb') as handle:
        head = handle.read(16)
    return any(head.startswith(magic) for magic in BUNDLE_MAGIC)


def bundle_heads(path):
    """返回 [(ref, sha)]，bundle 内记录的引用。"""
    process = subprocess.run(['git', 'bundle', 'list-heads', str(path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode != 0:
        raise GitError(f'读取 bundle 失败: {process.stderr.decode("utf-8", "replace").strip()}')
    heads = []
    for line in process.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) == 2:
            heads.append((parts[1], parts[0]))
    return heads


def bundle_prerequisites(path):
    """从 bundle 头部解析起点（prerequisite）commit，形如 `-<sha> <subject>`。"""
    prereqs = []
    with open(path, 'rb') as handle:
        for raw in handle:
            line = raw.decode('utf-8', 'replace').rstrip('\n')
            if not line:
                break
            if line.startswith('-'):
                sha = line[1:].split(' ', 1)[0].strip()
                if sha:
                    prereqs.append(sha)
    return prereqs


# --------------------------------------------------------------------------- backup

def cmd_backup(args):
    repo = Path(args.repo).resolve()
    base_name, base_sha = resolve_commit(repo, args.from_commit, '起点')

    ref = args.ref
    if not ref:
        symbolic = git(repo, ['symbolic-ref', '-q', '--short', 'HEAD'], check=False)
        if symbolic.returncode != 0:
            raise GitError('当前是 detached HEAD，请用 --ref 指定分支名')
        ref = symbolic.stdout.decode().strip()
    ref, ref_sha = resolve_commit(repo, ref, '目标引用')

    full_ref = git_out(repo, ['rev-parse', '--symbolic-full-name', ref], check=False)
    if not full_ref.startswith('refs/heads/') and not args.with_tags:
        raise GitError(f'目标引用 `{ref}` 不是本地分支（{full_ref or "裸提交"}），'
                       'bundle 里没有可还原的分支；请用 --ref 指定分支，'
                       '或加 --with-tags 把标签一并打包')
    if base_sha == ref_sha:
        raise GitError(f'{ref} 与起点 {base_name} 指向同一提交，没有可备份的增量')
    if git(repo, ['merge-base', '--is-ancestor', base_sha, ref_sha], check=False).returncode != 0:
        raise GitError(f'{base_name} 不是 {ref} 的祖先：只能对同一条线上的提交做增量备份')
    count = int(git_out(repo, ['rev-list', '--count', f'{base_sha}..{ref_sha}']))
    if count == 0:
        raise GitError(f'{base_name} 之后没有新提交')

    output = Path(args.output).resolve() if args.output else (
        Path.cwd() / f'git-history-{repo.name}-{ref.replace("/", "-")}-incremental.bundle')
    output.parent.mkdir(parents=True, exist_ok=True)

    log(f'仓库 {repo}；起点 {base_sha[:12]}（{base_name}）→ {ref}，共 {count} 个提交')
    command = ['bundle', 'create', str(output), f'{base_sha}..{ref}']
    if args.with_tags:
        command.append('--tags')
    process = git(repo, command, check=False)
    if process.returncode != 0:
        raise GitError('生成增量 bundle 失败: ' +
                       process.stderr.decode('utf-8', 'replace').strip())

    verify = git(repo, ['bundle', 'verify', str(output)], check=False)
    if verify.returncode != 0:
        raise GitError(verify.stderr.decode('utf-8', 'replace').strip())

    log(f'完成: {output}（{human_size(output.stat().st_size)}，{count} 个提交）')
    for name, sha in bundle_heads(output):
        log(f'  {name}  {sha}')
    log(f'起点 {base_sha}：还原目标必须包含该提交')


# --------------------------------------------------------------------------- baseline

def cmd_baseline(args):
    """为起点增量准备一个含起点快照的仓库；起点可以是 tag/分支，也可以是裸 commit SHA。"""
    repo = Path(args.repo).resolve()
    name, base_sha = resolve_commit(repo, args.from_commit, '起点')
    full_name = git_out(repo, ['rev-parse', '--symbolic-full-name', name], check=False)
    named = full_name.startswith(('refs/heads/', 'refs/tags/'))

    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()) and not args.force:
        raise GitError(f'目标目录非空: {dest}（确认覆盖请加 --force）')

    url = str(repo) if args.fast else Path(repo).as_uri()
    log(f'准备起点 {base_sha[:12]}（{name}）→ {dest}')

    if args.fast:
        log(f'来源 {url}（--fast：本地克隆，复制整个 pack，包含起点之后的提交）')
        command = ['git', 'clone', '--no-checkout', '--local']
        if named:
            command += ['--branch', name]
        command += [url, str(dest)]
        if subprocess.run(command).returncode != 0:
            raise GitError('本地克隆失败，请检查路径/权限')
        git(dest, ['update-ref', 'refs/git-history/baseline', base_sha])
        git(dest, ['update-ref', '--no-deref', 'HEAD', base_sha])
    elif named:
        log(f'来源 {url}（传输协议按起点浅拉取，大仓库需要几分钟）')
        command = ['git', 'clone', '--no-checkout', '--depth', '1', '--branch', name, url, str(dest)]
        if subprocess.run(command).returncode != 0:
            raise GitError('克隆起点失败，请检查网络/路径/凭据')
    else:
        # 裸 commit SHA：clone --branch 不支持，改为 init + 按 SHA 拉取单个提交
        log(f'来源 {url}（按 commit SHA 浅拉取；源需允许按 SHA fetch）')
        dest.mkdir(parents=True, exist_ok=True)
        git(dest, ['init', '-q'])
        fetch = git(dest, ['-c', 'uploadpack.allowAnySHA1InWant=true',
                           'fetch', '--depth', '1', url, base_sha], check=False)
        if fetch.returncode != 0:
            raise GitError('按 commit 拉取起点失败（源可能禁止按 SHA fetch）：\n'
                           + fetch.stderr.decode('utf-8', 'replace').strip()
                           + '\n可改用 tag/分支名作为 --from，或加 --fast（仅限本机仓库）')
        git(dest, ['update-ref', 'refs/git-history/baseline', base_sha])
        git(dest, ['update-ref', '--no-deref', 'HEAD', base_sha])

    if git_out(dest, ['cat-file', '-t', f'{base_sha}^{{commit}}'], check=False) != 'commit':
        raise GitError(f'起点准备结果异常: 目标仓库中没有提交 {base_sha}')
    log(f'起点就绪: {dest}（含 {base_sha}，未检出工作区）')
    log(f'下一步: backup --repo <仓库> --from {base_sha} -o <bundle>')


# --------------------------------------------------------------------------- restore

def cmd_restore(args):
    archive = Path(args.archive).resolve()
    if not archive.is_file():
        raise GitError(f'归档不存在: {archive}')
    if not looks_like_bundle(archive):
        raise GitError(f'{archive.name} 不是起点增量 bundle。\n'
                       '本工具只处理由 backup --from 生成的 .bundle 归档。')

    dest = Path(args.dest).resolve()
    if not is_repo(dest):
        raise GitError(f'还原目标不是 Git 仓库: {dest}\n'
                       f'请先执行: baseline --repo <仓库> --from <起点> -d {dest}')

    prereqs = bundle_prerequisites(archive)
    missing = [sha for sha in prereqs
               if git(dest, ['cat-file', '-e', sha], check=False).returncode != 0]
    if missing:
        raise GitError('目标仓库缺少起点提交:\n  ' + '\n  '.join(missing)
                       + f'\n请先执行: baseline --repo <仓库> --from {missing[0]} -d {dest}')

    verify = git(dest, ['bundle', 'verify', str(archive)], check=False)
    if verify.returncode != 0:
        raise GitError('bundle 校验失败:\n'
                       + (verify.stderr + verify.stdout).decode('utf-8', 'replace').strip())

    heads = bundle_heads(archive)
    conflicts = []
    for name, sha in heads:
        current = git(dest, ['rev-parse', '--verify', '-q', name], check=False)
        if current.returncode == 0:
            actual = current.stdout.decode().strip()
            if actual != sha:
                conflicts.append((name, actual, sha))
    if conflicts and not args.force:
        lines = '\n'.join(f'  {name}: 目标 {old[:12]} → 归档 {new[:12]}' for name, old, new in conflicts)
        raise GitError('目标仓库已有同名引用且指向不同提交，确认覆盖请加 --force：\n' + lines)

    refspecs = []
    if any(name.startswith('refs/heads/') for name, _ in heads):
        refspecs.append('+refs/heads/*:refs/heads/*')
    if any(name.startswith('refs/tags/') for name, _ in heads):
        refspecs.append('+refs/tags/*:refs/tags/*')
    if not refspecs:
        raise GitError('bundle 中没有可还原的分支或标签')

    # git fetch 拒绝更新“当前检出的分支”，先切成 detached HEAD 再导入
    current_head = git_out(dest, ['symbolic-ref', '-q', '--short', 'HEAD'], check=False)
    updating = {name[len('refs/heads/'):] for name, _ in heads if name.startswith('refs/heads/')}
    if current_head and current_head in updating:
        log(f'目标当前检出 {current_head}，先切到 detached HEAD 再更新分支')
        git(dest, ['checkout', '--detach'])

    log(f'导入 {len(heads)} 个引用 → {dest}（起点 {", ".join(sha[:12] for sha in prereqs) or "无"}）')
    fetch = git(dest, ['fetch', '--no-tags', str(archive), *refspecs], check=False)
    if fetch.returncode != 0:
        raise GitError('git fetch 失败:\n' + fetch.stderr.decode('utf-8', 'replace').strip())

    bad = []
    for name, sha in heads:
        actual = git_out(dest, ['rev-parse', '--verify', name], check=False)
        if actual != sha:
            bad.append(f'{name}: 期望 {sha} 实际 {actual or "（缺失）"}')
    if bad:
        for line in bad:
            warn(line)
        raise GitError('还原后引用 hash 不一致')
    log(f'校验通过：{len(heads)} 个引用 hash 与归档一致（对象原字节导入，hash 不变）')

    branch = next((name for name, _ in heads if name.startswith('refs/heads/')), None)
    short = branch[len('refs/heads/'):] if branch else None
    if args.checkout and short and git_out(dest, ['rev-parse', '--is-bare-repository']) == 'true':
        warn('目标是裸仓库，跳过工作区检出')
    elif args.checkout and short:
        git(dest, ['checkout', '-f', short])
        log(f'已检出 {short}（工作区在 {dest}）')
    elif short:
        log(f'未检出工作区（需要文件请加 --checkout，或执行: git -C {dest} checkout -f {short}）')


# --------------------------------------------------------------------------- verify

def cmd_verify(args):
    archive = Path(args.archive).resolve()
    if not archive.is_file() or not looks_like_bundle(archive):
        raise GitError(f'{archive} 不是起点增量 bundle')
    repo = Path(args.repo).resolve()
    if not is_repo(repo):
        raise GitError(f'仓库不存在: {repo}')

    prereqs = bundle_prerequisites(archive)
    heads = bundle_heads(archive)
    bad = []
    for sha in prereqs:
        if git(repo, ['cat-file', '-e', sha], check=False).returncode != 0:
            bad.append(f'缺少起点对象 {sha}')
    for name, sha in heads:
        actual = git_out(repo, ['rev-parse', '--verify', '-q', name], check=False)
        if actual != sha:
            bad.append(f'{name}: 期望 {sha} 实际 {actual or "（缺失）"}')
    if bad:
        for line in bad:
            warn(line)
        print('[FAIL] 校验失败', file=sys.stderr)
        raise SystemExit(1)
    log(f'校验通过：起点 {", ".join(sha[:12] for sha in prereqs) or "无"}，'
        f'{len(heads)} 个引用 hash 一致')


# --------------------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        description='Git 提交记录起点增量备份/还原（保持 commit id hash 不变）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例:\n'
               '  # 1. 在还原目标准备起点（SHA / tag / 分支均可）\n'
               '  python3 scripts/git_history.py baseline --repo src --from 0fa6d91e2f -d /data/base\n'
               '  # 2. 备份起点之后的提交（KB 级）\n'
               '  python3 scripts/git_history.py backup --repo src --from 0fa6d91e2f -o inc.bundle\n'
               '  # 3. 还原并校验\n'
               '  python3 scripts/git_history.py restore -a inc.bundle -d /data/base --checkout\n'
               '  python3 scripts/git_history.py verify -a inc.bundle -r /data/base\n')
    sub = parser.add_subparsers(dest='command', required=True)

    backup = sub.add_parser('backup', help='备份起点之后的提交为增量 bundle')
    backup.add_argument('-r', '--repo', default='.', help='仓库路径（默认当前目录）')
    backup.add_argument('--from', '--base', dest='from_commit', required=True,
                        help='起点 commit（SHA/tag/分支）')
    backup.add_argument('--ref', help='目标分支（默认当前分支）')
    backup.add_argument('-o', '--output', help='输出 bundle 路径')
    backup.add_argument('--with-tags', action='store_true', help='同时打包指向增量范围的标签')
    backup.set_defaults(func=cmd_backup)

    baseline = sub.add_parser('baseline', help='在还原目标准备只含起点快照的仓库')
    baseline.add_argument('-r', '--repo', default='.', help='起点来源仓库（默认当前目录）')
    baseline.add_argument('--from', '--base', dest='from_commit', required=True,
                          help='起点 commit（SHA/tag/分支）')
    baseline.add_argument('-d', '--dest', required=True, help='还原目标目录（不存在或为空）')
    baseline.add_argument('--fast', action='store_true', help='本地克隆：快，但复制整个 pack')
    baseline.add_argument('--force', action='store_true', help='目标目录非空时继续')
    baseline.set_defaults(func=cmd_baseline)

    restore = sub.add_parser('restore', help='把增量 bundle 导入含起点的仓库')
    restore.add_argument('-a', '--archive', required=True, help='增量 bundle 路径')
    restore.add_argument('-d', '--dest', required=True, help='还原目标仓库')
    restore.add_argument('--checkout', action='store_true', help='导入后检出分支工作区')
    restore.add_argument('--force', action='store_true', help='覆盖目标已有同名引用')
    restore.set_defaults(func=cmd_restore)

    verify = sub.add_parser('verify', help='核对仓库是否含起点且引用 hash 一致')
    verify.add_argument('-a', '--archive', required=True, help='增量 bundle 路径')
    verify.add_argument('-r', '--repo', default='.', help='待核对的仓库（默认当前目录）')
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except GitError as error:
        print(f'[ERROR] {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('[ERROR] 已中断', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
