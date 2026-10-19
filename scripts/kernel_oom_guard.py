"""内核 CPU/OOM 护栏校验：zygote 子进程必须从薄壳 arupa_render 起。

为什么需要这个文件：2026-10-15 那次「内核 CPU 拉满 → 内存涨到 OOM」的根因是
库式嵌入下 `PathService::FILE_EXE` 指向 .NET apphost，而 apphost 的托管入口不派发
`--type=zygote`，于是每个 zygote 又完整跑一遍浏览器、每层再 spawn 两个 zygote
（二叉树爆炸，观测到 326 个）。护栏有两层，都在 `arupa_desktop` / `nomad_desktop`
这两个 overlay 仓库里；**它们是未提交改动，一次 pull 就会被静默清掉**（2026-10-16
真实发生过：修复没了，包照出，行为退回 OOM）。所以除了构建前的
`build.report_dirty_project` 告警之外，还需要一个能对**产物**发问的东西。

用法：
  python3 scripts/kernel_oom_guard.py static                # 扫最新交付 + 最新 UI payload
  python3 scripts/kernel_oom_guard.py static --delivery dist/arupa-linux-...-static-49
  python3 scripts/kernel_oom_guard.py run --payload dist/nomad-linux-...-release-50/arupa-desktop
  python3 scripts/kernel_oom_guard.py run --payload <dir> --headless   # 无 X 也能跑

static 只读产物字节找护栏标志（秒级）；run 用假 UI + NOMAD_HOST_MAXMS 有界启动，
硬闸（进程数/总 RSS/墙钟）任一超限即 killpg —— 绝不让校验本身把机器吃进去。
"""
import argparse
import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch as F  # noqa: E402  （ROOT / F.log / F.err 与其它脚本同源）

# 护栏标志（按「文件里能搜到的字面量」判，不绑定实现细节：改写法可以，但两层必须至少
# 有一层在，否则 zygote 又会被 apphost 接走）。
KERNEL_MARKERS = {
    # 交付件 platform -> [(字面量, 说明), ...]
    'linux': [
        (b'cannot point FILE_EXE at', 'FILE_EXE 改指薄壳（失败即拒绝启动）'),
        (b'no usable sandbox', '无可用沙箱时自动降级 --no-sandbox（否则 ZygoteHostImpl FATAL）'),
    ],
    'mac': [
        (b'cannot point FILE_EXE at', 'FILE_EXE 改指薄壳（失败即拒绝启动）'),
    ],
    'win': [
        (b'cannot point FILE_EXE at', 'FILE_EXE 改指薄壳（失败即拒绝启动）'),
    ],
}
HOST_MARKERS = [
    ('handing off to', '托管入口把 --type=… 交棒给薄壳'),
    ('refusing to run the full', '薄壳缺失时拒绝把完整浏览器当子进程跑'),
]
KERNEL_LIBRARY = {'linux': 'libarupa_kernel.so', 'mac': 'libarupa_kernel.dylib',
                  'win': 'arupa_kernel.dll'}
SUBPROCESS_HOST = {'linux': 'arupa_render', 'mac': 'arupa_render', 'win': 'arupa_render.exe'}

CHUNK = 8 << 20
CAP_PROCS = 15          # 正常形态 ≈ 6-10（宿主 + 2 zygote + gpu + renderer + utility）
CAP_RSS_GB = 3.0
HARD_PROCS = 40         # 硬闸：超过即 killpg
HARD_RSS_GB = 8.0
HOST_SECONDS = 15       # 宿主自退出时长（NOMAD_HOST_MAXMS），与 2026-10-16 harness 一致
SUPERVISOR_MARGIN = 12  # 监控窗口比宿主自退出多留这么多秒，免得把「正常自退出」判成超时
POLL = 0.25


def _count_marker(path, marker, encoding='latin-1'):
    """在文件里数字面量出现次数（分块读，块间留 marker 长度的重叠，跨块也算得到）。"""
    if not os.path.isfile(path):
        return 0
    overlap = max(len(marker) - 1, 0)
    count = 0
    tail = b''
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            window = tail + chunk
            count += window.count(marker)
            tail = window[-overlap:] if overlap else b''
    return count


def _utf16le(text):
    return text.encode('utf-16-le')


def scan_kernel(path, target_os='linux'):
    """内核 .so/.dylib 里的护栏标志。"""
    return [(marker.decode('latin-1'), note, _count_marker(path, marker))
            for marker, note in KERNEL_MARKERS[target_os]]


def scan_host_dll(path):
    """托管宿主 dll 里的护栏标志（.NET 字面量是 UTF-16LE，不能按 ASCII 搜）。"""
    return [(text, note, _count_marker(path, _utf16le(text)))
            for text, note in HOST_MARKERS]


def report(entries, title):
    print(f'  [{title}]')
    for name, note, count in entries:
        flag = '✔' if count else '✘'
        print(f'    {flag} {name!r} × {count} — {note}')
    return all(count for _name, _note, count in entries)


def verify_delivery(delivery, target_os='linux'):
    """内核交付目录：护栏标志 + 薄壳是否随包发出。"""
    kernel_dir = os.path.join(delivery, 'kernel')
    library = os.path.join(kernel_dir, KERNEL_LIBRARY[target_os])
    host = os.path.join(kernel_dir, SUBPROCESS_HOST[target_os])
    print(f'内核交付 {delivery}')
    ok = report(scan_kernel(library, target_os), os.path.basename(library))
    if os.path.isfile(host):
        print(f'    ✔ 子进程薄壳 {SUBPROCESS_HOST[target_os]} 随包发出 '
              f'({os.path.getsize(host)}B)')
    else:
        print(f'    ✘ 缺子进程薄壳 {host}（库式嵌入下子进程一个都起不来）')
        ok = False
    return ok


def find_payload(root, target_os='linux', depth=6):
    """交付目录里找到「真正被跑起来」的那一层（含内核库的那一层）。

    布局随平台变（linux 是 <交付>/arupa-desktop、mac 是 .app/Contents/...），所以按
    内核库文件定位，而不是硬编码路径。

    mac 实际布局是 <交付>/逐风浏览器.app/Contents/Resources/arupa-mac/libarupa_kernel.dylib，
    内核库在**第 3 层**目录里；早先 depth=3 会在走到 arupa-mac 之前就剪枝，把齐备的交付
    误报成「找不到 libarupa_kernel.dylib」。留够余量到 6（.framework/Versions/A 也够）。
    """
    library = KERNEL_LIBRARY[target_os]
    if os.path.isfile(os.path.join(root, library)):
        return root
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.relpath(dirpath, root).count(os.sep) >= depth:
            dirnames[:] = []
            continue
        if library in filenames:
            return dirpath
    return None


def verify_payload(payload, target_os='linux'):
    """UI payload（真正被跑起来的那份）：内核 + 托管宿主 + 薄壳三件都查。"""
    library = os.path.join(payload, KERNEL_LIBRARY[target_os])
    host = os.path.join(payload, SUBPROCESS_HOST[target_os])
    print(f'UI payload {payload}')
    ok = report(scan_kernel(library, target_os), os.path.basename(library))
    dll = next((name for name in sorted(os.listdir(payload))
                if name.endswith('.KernelHost.dll')), None) if os.path.isdir(payload) else None
    if dll:
        ok = report(scan_host_dll(os.path.join(payload, dll)), dll) and ok
    else:
        print('    ✘ 找不到 *KernelHost.dll（托管护栏无从校验）')
        ok = False
    if os.path.isfile(host):
        print(f'    ✔ 子进程薄壳 {os.path.basename(host)} 就位 ({os.path.getsize(host)}B)')
    else:
        print(f'    ✘ 缺子进程薄壳 {host}')
        ok = False
    return ok


def verify_payload_tree(root, target_os='linux'):
    """给交付目录也能用：先定位 payload 层，再校验。"""
    payload = find_payload(str(root), target_os)
    if payload is None:
        print(f'    ✘ {root} 里找不到 {KERNEL_LIBRARY[target_os]}')
        return False
    return verify_payload(payload, target_os)


def newest(kind, target_os='linux', arch='x64'):
    """最新交付 / payload 目录（序号最大的那份）。"""
    pattern = {'delivery': re.compile(rf'arupa-{target_os}-{arch}-.+-static-(\d+)$'),
               'payload': re.compile(rf'nomad-{target_os}-{arch}-.+-release-(\d+)$')}[kind]
    choices = []
    for path in (F.WORKSPACE_ROOT / 'dist').glob('*'):
        if not path.is_dir():
            continue
        match = pattern.fullmatch(path.name)
        if match:
            choices.append((int(match[1]), path))
    return max(choices, key=lambda pair: pair[0])[1] if choices else None


# ---------------------------------------------------------------- 有界实测部分

def fake_ui(sock_path, stop, deadline):
    """假 UI：回 "X ok"，让 host 的认证/输入通道活下来（同 2026-10-16 harness）。"""
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(4)
    server.settimeout(1.0)
    while not stop.is_set() and time.time() < deadline:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        conn.settimeout(0.5)
        buf = b''
        while not stop.is_set() and time.time() < deadline:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                # 静默 ≠ 断开：保持连接（断了 host 会当成 UI 退出而提前收尾）。
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                if line.startswith(b'X '):
                    try:
                        conn.sendall(b'X ok\n')
                    except OSError:
                        break
        conn.close()
    server.close()


def sample_tree(pgid):
    """进程组内每个进程的 (pid, exe, rss, --type, cmdline)。"""
    out = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f'/proc/{pid}/stat', 'rb') as handle:
                fields = handle.read().rsplit(b')', 1)[1].split()
            if int(fields[2]) != pgid:
                continue
            with open(f'/proc/{pid}/statm') as handle:
                rss = int(handle.read().split()[1]) * 4096
            exe = os.path.basename(os.readlink(f'/proc/{pid}/exe'))
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                    argv = handle.read().split(b'\0')
            except OSError:
                argv = []
            cmdline = ' '.join(a.decode('utf-8', 'replace') for a in argv if a)[:200]
            ptype = next((part for part in cmdline.split() if part.startswith('--type=')), '')
            out.append((pid, exe, rss, ptype, cmdline))
        except (FileNotFoundError, ProcessLookupError, IndexError, PermissionError):
            continue
    return out


def verdict(host_exe, shell_exe, host_peak, child_exes, child_types, peak_procs, peak_rss,
            exit_code, log_text):
    """把一次实测的采样归纳成 (ok, 失败原因列表)。抽出来是为了能单测。"""
    problems = []
    if host_peak != 1:
        problems.append(f'KernelHost 实例峰值 {host_peak} ≠ 1（>1 就是宿主被当子进程拉起来的形态）')
    wrong = sorted({exe for exe in child_exes if exe != shell_exe})
    if wrong:
        problems.append('有子进程不是薄壳: ' + ', '.join(wrong))
    if not any(ptype == '--type=zygote' for ptype in child_types):
        problems.append('没观察到 --type=zygote 子进程（护栏路径根本没被走到，结论不可信）')
    if peak_procs > CAP_PROCS:
        problems.append(f'进程数峰值 {peak_procs} > {CAP_PROCS}')
    if peak_rss > CAP_RSS_GB:
        problems.append(f'总 RSS 峰值 {peak_rss:.2f}GB > {CAP_RSS_GB}GB')
    if exit_code != 0:
        problems.append(f'宿主退出码 {exit_code} ≠ 0')
    if 'No usable sandbox!' in log_text:
        problems.append('日志里有无可用沙箱 FATAL')
    return (not problems), problems


def run_guard(payload, target_os='linux', display=None, headless=False,
              wall_seconds=HOST_SECONDS, work=None):
    """有界实测：假 UI + NOMAD_HOST_MAXMS 自退出 + 硬闸。返回 (ok, 说明行列表)。"""
    # 绝对化：下面给 Popen 传了 cwd=payload，相对路径会按新 cwd 解析而找不到可执行文件。
    payload = os.path.abspath(payload)
    # 工作目录放在 out/ 而不是 payload 里：交付目录有自己的清单/SHA256SUMS，别往里塞东西。
    work = work or os.path.join(str(F.WORKSPACE_ROOT / 'out'), 'oom-guard',
                                os.path.basename(payload))
    os.makedirs(work, exist_ok=True)
    sock_path = os.path.join(work, 'ui.sock')
    log_path = os.path.join(work, 'host.log')
    finished = threading.Event()
    window = wall_seconds + SUPERVISOR_MARGIN
    thread = threading.Thread(target=fake_ui,
                              args=(sock_path, finished, time.time() + window),
                              daemon=True)
    thread.start()
    time.sleep(0.4)

    env = {k: v for k, v in os.environ.items() if not k.startswith(('NOMAD_', 'ARUPA_'))}
    env.update({
        'ARUPA_OUT': payload,
        'NOMAD_SHM': os.path.join(work, 'frame.shm'),
        'NOMAD_POPUP_SHM': os.path.join(work, 'popup.shm'),
        'NOMAD_SOCK': sock_path,
        # 必须是 32 字节十六进制，否则宿主直接 Unhandled exception。
        'NOMAD_IPC_TOKEN': os.urandom(32).hex(),
        'NOMAD_HOST_MAXMS': str(int(wall_seconds * 1000)),
        'NOMAD_USER_DATA_ROOT': work,
        'NOMAD_PROFILE_DIR': os.path.join(work, 'profile'),
        # 1280x760 / zh-CN：与人工 harness 同一份启动配置，避免走 UI 的默认分支。
        'NOMAD_LAUNCH_CONFIG': base64.b64encode(json.dumps({
            'version': 1, 'width': 1280, 'height': 760, 'deviceScaleFactor': 1.0,
            'offTheRecord': False, 'partitionName': None, 'interceptDownloads': False,
            'locale': 'zh-CN', 'fingerprintHardening': False}).encode()).decode(),
    })
    if headless:
        env['ARUPA_OZONE_PLATFORM'] = 'headless'
    elif display:
        env['DISPLAY'] = display

    host = os.path.join(payload, SUBPROCESS_HOST.get(target_os) or 'arupa_render')
    apphost = os.path.join(payload, {'linux': 'NomadBrowser.Linux.KernelHost',
                                     'mac': 'NomadBrowser.Mac.KernelHost'}.get(target_os, ''))
    for path in (apphost, host):
        if not os.path.isfile(path):
            return False, [f'缺 {path}']
    with open(log_path, 'wb') as log:
        child = subprocess.Popen([apphost], cwd=payload, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
    pgid = os.getpgid(child.pid)
    started = time.time()
    peak_procs = peak_hosts = 0
    peak_rss = 0.0
    prev_ticks = prev_time = 0.0
    started_at = time.time()
    peak_cpu = 0.0
    child_exes, child_types = set(), set()
    tripped = ''
    while time.time() - started < window:
        now = time.time()
        procs = sample_tree(pgid)
        rss = sum(p[2] for p in procs) / 2 ** 30
        ticks = sum(_ticks(p[0]) for p in procs)
        peak_cpu = max(peak_cpu, max(0.0, (ticks - prev_ticks) / max(now - prev_time, 1e-6)) * 100)
        prev_ticks, prev_time = ticks, now
        for pid, exe, _rss, ptype, _cmdline in procs:
            if exe.startswith(('NomadBrowser',)):
                continue
            child_exes.add(exe)
            if ptype:
                child_types.add(ptype)
        peak_hosts = max(peak_hosts, sum(1 for p in procs if p[1].startswith('NomadBrowser')))
        peak_procs = max(peak_procs, len(procs))
        peak_rss = max(peak_rss, rss)
        if len(procs) > HARD_PROCS:
            tripped = f'进程数 {len(procs)} > {HARD_PROCS}'
            break
        if rss > HARD_RSS_GB:
            tripped = f'总 RSS {rss:.1f}GB > {HARD_RSS_GB}GB'
            break
        if child.poll() is not None and not procs:
            break
        time.sleep(POLL)
    if tripped:
        print(f'  ⛔ 硬闸触发：{tripped} → killpg（这就是失败形态，已止损）')
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=10)
    finished.set()
    time.sleep(0.3)
    # 共享内存是 2×128MB + 128MB 量级：跑完就删，只留 host.log 当证据。
    for leftover in ('frame.shm', 'popup.shm'):
        try:
            os.unlink(os.path.join(work, leftover))
        except OSError:
            pass

    log_text = open(log_path, 'r', errors='replace').read()
    host_exe = os.path.basename(apphost)
    shell_exe = os.path.basename(host)
    ok, problems = verdict(host_exe, shell_exe, peak_hosts, child_exes, child_types,
                           peak_procs, peak_rss, child.returncode, log_text)
    lines = [f'宿主 exit={child.returncode}，墙钟 {time.time() - started:.1f}s',
             f'峰值：进程数 {peak_procs}，总 RSS {peak_rss:.2f}GB，宿主实例 {peak_hosts}，'
             f'CPU ≈ {peak_cpu:.0f}%（单核）',
             f'子进程 exe：{sorted(child_exes) or "（无）"}',
             f'子进程 --type：{sorted(child_types) or "（无）"}',
             f'日志 {log_path}']
    if tripped:
        problems.insert(0, f'硬闸触发（{tripped}）')
    return ok, lines + [f'✘ {problem}' for problem in problems]


def _ticks(pid):
    try:
        with open(f'/proc/{pid}/stat', 'rb') as handle:
            fields = handle.read().rsplit(b')', 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
    except (OSError, IndexError):
        return 0.0


def report_static(target, target_os='linux', what='payload'):
    """打包后调用（build.py 传内核交付、nomad.py 传 UI 交付）：只告警，不拦构建。

    合法实现方式可能变（判据是"标志在不在"，不是"代码必须长这样"），所以失败只喊一声；
    真要判定行为，用 `run` 子命令实测。
    """
    try:
        ok = (verify_delivery(str(target), target_os) if what == 'delivery'
              else verify_payload_tree(target, target_os))
    except (OSError, KeyError) as exc:
        F.log(f'⚠ 内核护栏静态校验跳过：{exc}')
        return
    if ok:
        F.log(f'✔ 内核护栏就位（zygote→{SUBPROCESS_HOST[target_os]}）：{target}')
    else:
        F.log(f'⚠ {target} 里没找到完整的内核护栏标志 —— 若这份内核的 zygote 会被 NomadBrowser '
              f'宿主接过，就是 2026-10-15 那次 CPU/OOM 的形态（见 debug-nomad-cpu-oom.md）。'
              f'用 python3 scripts/kernel_oom_guard.py run --payload <UI payload> 实测确认。')


def report_delivery(delivery, target_os='linux'):
    report_static(delivery, target_os, 'delivery')


def report_payload(delivery, target_os='linux'):
    report_static(delivery, target_os, 'payload')


def main():
    parser = argparse.ArgumentParser(description='内核 CPU/OOM 护栏校验')
    parser.add_argument('mode', choices=['static', 'run'])
    parser.add_argument('--os', default='linux', choices=['linux', 'mac', 'win'])
    parser.add_argument('--delivery', default=None,
                        help='内核交付目录（默认取最新；传空串 = 跳过这一项）')
    parser.add_argument('--payload', default=None,
                        help='UI payload/交付目录（默认取最新；传空串 = 跳过这一项）')
    parser.add_argument('--arch', default='x64')
    parser.add_argument('--display', default='', help='X display，如 :97')
    parser.add_argument('--headless', action='store_true', help='ARUPA_OZONE_PLATFORM=headless')
    parser.add_argument('--seconds', type=int, default=HOST_SECONDS,
                        help=f'宿主自退出时长（默认 {HOST_SECONDS}s，监控窗口自动 +{SUPERVISOR_MARGIN}s）')
    args = parser.parse_args()

    delivery = args.delivery if args.delivery is not None else (
        str(newest('delivery', args.os, args.arch) or ''))
    payload = args.payload if args.payload is not None else (
        str(newest('payload', args.os, args.arch) or ''))
    if args.mode == 'static':
        ok = True
        # 两个目标都按「有没有 kernel/ 子目录」自动分辨是内核交付还是 UI 交付，
        # 免得传错目录时给出无意义的结论。
        for target in (delivery, payload):
            if not target:
                continue
            if os.path.isdir(os.path.join(target, 'kernel')):
                ok = verify_delivery(target, args.os) and ok
            else:
                ok = verify_payload_tree(target, args.os) and ok
        if not delivery and not payload:
            F.err('dist/ 下找不到内核交付或 UI payload')
    else:
        if not payload:
            F.err('没有 payload 可跑（先打包 nomad_desktop，或用 --payload 指定）')
        ok, lines = run_guard(payload, args.os, args.display or None, args.headless,
                              args.seconds)
        print('\n'.join('  ' + line for line in lines))
    print(('✔ 护栏就位' if ok else '✘ 护栏缺失/形态不对'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
