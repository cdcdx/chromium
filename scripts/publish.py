"""内核交付包上传：把 dist/ 下的 arupa 交付 zip 推到发布服务器。

协议与 scripts/push-http.mjs 同源（分块、可续传、失败重试），只用标准库：

  1. POST /api/uploads               建会话（元数据 platform / version / filename / size / sha256）
  2. GET  /api/uploads/<id>          先查服务端权威偏移（上一次响应可能丢）
  3. PUT  /api/uploads/<id>          从该偏移续传一块（Upload-Offset + Content-Length）
  4. POST /api/uploads/<id>/complete 收尾校验

中途断开后重跑同一条命令即可从服务端记录的 offset 接着传，不会重头再来。

与 push-http.mjs 的差别（刻意的）：
  * 令牌取自环境变量 / .env 的 publish_upload_token，不再读 --token-file。
  * 服务端返回的分块大小非法、源文件在传输中被改动这类协议错误不重试，
    立刻报错（原实现会把它们也重试 4 次，纯属空等）。
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import stat
import time
from typing import NamedTuple
import urllib.error
import urllib.parse
import urllib.request

import fetch as F

# ── 协议参数（与 push-http.mjs 保持一致）───────────────────────────────────
MAX_CHUNK = 64 * 1024 * 1024          # 单块上限；服务端给的值必须落在 (0, MAX_CHUNK]
REQUEST_TIMEOUT = 30 * 60             # 单次请求 30 分钟（大分块慢链路）
RETRY_STATUS = (409, 500, 502, 503, 504)
MAX_ATTEMPTS = 5                      # 首次 + 4 次重试
RETRY_BASE = 0.5                      # 秒
RETRY_CAP = 5.0

DESKTOP_FORMAT = "arupa-<os>-<arch>-<ver>-<static|dynamic>-<n>.zip"
ANDROID_FORMAT = "arupa-android-<ver>-<static|dynamic>-<n>.zip"
_DESKTOP_RE = re.compile(
    r"arupa-(win|mac|linux)-(x86|x64|arm64)-(\d+\.\d+\.\d+\.\d+)-(static|dynamic)-(\d+)\.zip")
_ANDROID_RE = re.compile(r"arupa-android-(\d+\.\d+\.\d+\.\d+)-(static|dynamic)-(\d+)\.zip")
_TRAILING_NUMBER = re.compile(r"(\d+)$")


class ApiError(RuntimeError):
    """服务端/网络错误。status 为空表示没拿到 HTTP 响应（连接层失败）。"""

    def __init__(self, message, status=None, retryable=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ── 交付包命名 ─────────────────────────────────────────────────────────────
class Artifact(NamedTuple):
    target_os: str
    arch: str
    version: str
    link: str
    build: int


def parse_artifact(name):
    """按交付包命名解析 os/arch/version/link/build（与 package-arupa_*.sh 同源约定）。"""
    match = _DESKTOP_RE.fullmatch(name)
    if match:
        return Artifact(match[1], match[2], match[3], match[4], int(match[5]))
    match = _ANDROID_RE.fullmatch(name)
    if match:
        return Artifact("android", "", match[1], match[2], int(match[3]))
    raise RuntimeError(f"交付包命名不符合约定: {name}\n"
                       f"  桌面:  {DESKTOP_FORMAT}\n"
                       f"  Android: {ANDROID_FORMAT}")


def platform_of(name):
    """元数据 platform：即文件名里的系统名（mac / win / linux / android）。

    服务端会拿它与文件名交叉校验，架构不属于这一项（架构也从文件名解析）。"""
    return parse_artifact(name).target_os


def delivery_prefix(target_os, arch, version, link):
    """dist/ 里同身份交付的公共前缀；与 build.py 的 kernel_delivery_prefix 同源。"""
    if target_os == "android":
        return f"arupa-android-{version}-{link}-"
    return f"arupa-{target_os}-{arch}-{version}-{link}-"


# ── 交付包选取 ─────────────────────────────────────────────────────────────
def selector(file_value, num):
    """把 --file 归一成「路径」或「序号」。--file 给纯数字时按交付序号理解。"""
    if file_value:
        text = str(file_value)
        if text.isdigit():
            return int(text)
        return Path(text).expanduser()
    return num


def select_archive(dist_dir, prefix, requested=None):
    """选交付包：显式路径 > 交付序号 > 体积最大的 zip。

    默认口径是「最大的压缩包」；同体积时取序号更大的那份。命中不了同身份前缀时
    退回 dist/ 下体积最大的 zip，并明确提示（避免静默推错东西）。"""
    if isinstance(requested, Path):
        path = requested if requested.is_absolute() else Path.cwd() / requested
        if not path.is_file():
            path = dist_dir / requested.name
        if not path.is_file():
            F.err(f"找不到交付包: {requested}")
        return path
    if isinstance(requested, int):
        path = dist_dir / f"{prefix}{requested}.zip"
        if not path.is_file():
            F.err(f"找不到交付包: {path}\n" + _listing(dist_dir, prefix))
        return path
    everything = sorted(p for p in dist_dir.glob("*.zip") if p.is_file())
    matching = [p for p in everything if p.name.startswith(prefix)]
    if not matching and everything:
        F.log(f"注意: {dist_dir} 下没有 {prefix}<n>.zip，改用体积最大的 zip")
    pool = matching or everything
    if not pool:
        F.err(f"{dist_dir} 下没有任何 .zip；请先执行 package（需带 --zip）")
    return max(pool, key=lambda item: (item.stat().st_size, _trailing_number(item.name)))


def _trailing_number(name):
    match = _TRAILING_NUMBER.search(name[: -len(".zip")])
    return int(match[1]) if match else 0


def _listing(dist_dir, prefix):
    available = sorted(p.name for p in dist_dir.glob("*.zip") if p.is_file())
    if not available:
        return f"  {dist_dir} 下没有任何 .zip；请先执行 package（需带 --zip）"
    return "  现有 zip:\n" + "\n".join(f"    {name}" for name in available)


# ── 站点校验与摘要 ─────────────────────────────────────────────────────────
LOOPBACK = ("localhost", "127.0.0.1", "::1")
# 内网/本机私有网段：走 http:// 属于日常用法，默认放行；其余地址要显式 --allow-http。
# （公网 http 会把令牌明文暴露在链路上，脚本不该替用户默认同意。）
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10", "fc00::/7", "fe80::/10",
))


def _is_private_host(hostname):
    """判断主机是否是回环/内网字面量。域名无法判定，返回 False。"""
    host = (hostname or "").strip("[]")
    if host in LOOPBACK:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def validate_origin(url, allow_http):
    """校验发布地址并归一成 scheme://host[:port]。"""
    if not url or not str(url).strip():
        F.err("缺少发布地址：--url，或 .env 里的 publish_url")
    parts = urllib.parse.urlsplit(str(url).strip())
    if (parts.username or parts.password or parts.query or parts.fragment
            or parts.scheme not in ("http", "https") or parts.path not in ("", "/")
            or not parts.netloc):
        F.err("--url 只能是不含凭据/路径/查询/片段的站点源，例如 http://192.168.77.104:8080")
    if parts.scheme == "http" and not allow_http and not _is_private_host(parts.hostname):
        F.err(f"http:// 会明文传输上传令牌，且 {parts.hostname} 不是内网地址；"
              "确认链路可信后加 --allow-http，或改用 https://")
    return f"{parts.scheme}://{parts.netloc}"


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ── HTTP ───────────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """重定向一律当错误（同 fetch 的 redirect:'error'）—— 避免令牌被带到别处。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "服务器返回重定向，已按策略拒绝", headers, fp)


def _call(opener, origin, endpoint, method, token, body=None, headers=None):
    request = urllib.request.Request(origin + endpoint, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    status, raw = None, b""
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        # 连接被对端截断时 http.client 抛的是 RemoteDisconnected / IncompleteRead，
        # 它们不是 URLError，但语义一样（响应没拿到），必须按可重试的网络错误处理。
        reason = getattr(error, "reason", error)
        raise ApiError(f"{method} {endpoint}: {reason}")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if status >= 400:
        detail = (payload or {}).get("error") or f"HTTP {status}"
        raise ApiError(f"{method} {endpoint}: {detail}", status, (payload or {}).get("retryable"))
    if not isinstance(payload, dict):
        raise ApiError(f"{method} {endpoint}: 服务器返回了非 JSON 响应（HTTP {status}）")
    return payload


def _retry(action, describe):
    attempt = 0
    while True:
        try:
            return action()
        except ApiError as error:
            retryable = ((error.status is None or error.status in RETRY_STATUS)
                         and error.retryable is not False)
            if attempt >= MAX_ATTEMPTS - 1 or not retryable:
                raise
            delay = min(RETRY_BASE * (2 ** attempt), RETRY_CAP)
            F.log(f"{describe}失败（{error}）；{delay:g}s 后重试"
                  f"（第 {attempt + 2}/{MAX_ATTEMPTS} 次）")
            time.sleep(delay)
            attempt += 1


# ── 上传 ───────────────────────────────────────────────────────────────────
def push(path, url="", token="", allow_http=False, platform="", version=""):
    """把交付包推到发布服务器；成功返回服务端会话对象。"""
    path = Path(path)
    info = parse_artifact(path.name)
    if not version:
        version = info.version
    if not token:
        F.err("缺少上传令牌：--token、环境变量 publish_upload_token，"
              "或 .env 里的 publish_upload_token")
    origin = validate_origin(url, allow_http)
    if origin.startswith("http://") and not allow_http:
        F.log(f"注意: {origin} 走 http:// 明文传输令牌（按内网地址放行；公网请用 https://）")
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        F.err(f"交付包必须是普通文件（不能是软链或目录）: {path}")
    platform = platform or platform_of(path.name)

    if F.DRY_RUN:
        F.log(f"(dry-run) 推送 {path.name} → {origin}"
              f"（platform={platform}, version={version}, size={before.st_size}）")
        return None

    F.log(f"计算 sha256: {path}")
    digest = sha256_file(path)
    metadata = {"platform": platform, "version": version, "filename": path.name,
                "size": before.st_size, "sha256": digest}
    F.log(f"推送 {path.name} → {origin}（platform={platform}, version={version}, "
          f"{before.st_size} 字节）")

    opener = urllib.request.build_opener(_NoRedirect)
    session = _retry(lambda: _call(opener, origin, "/api/uploads", "POST", token,
                                   json.dumps(metadata).encode("utf-8"),
                                   {"Content-Type": "application/json"}),
                     "建立上传会话")
    endpoint = f"/api/uploads/{session['id']}"
    total = before.st_size
    shown = -1

    def progress(offset):
        nonlocal shown
        if total <= 0:
            return
        percent = offset * 100 // total
        if percent != shown:
            shown = percent
            F.log(f"上传 {percent}%（{offset}/{total} 字节）")

    progress(session.get("offset") or 0)
    if not session.get("completed"):
        with path.open("rb") as stream:
            while True:
                session = _retry(lambda: _upload_chunk(opener, origin, endpoint, token,
                                                       stream, metadata, total),
                                 "上传分块")
                offset = session.get("offset") or 0
                progress(offset)
                if session.get("completed") or offset >= total:
                    break
        after = path.lstat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_size, before.st_mtime_ns, before.st_ctime_ns):
            F.err("源文件在传输过程中被改动；请冻结交付件后重试")

    result = _retry(lambda: _call(opener, origin, endpoint + "/complete", "POST", token),
                    "完成上传")
    F.log(f"已发布: {result.get('path') or metadata['filename']}")
    F.log(f"SHA256: {result.get('sha256') or digest}")
    return result


def _upload_chunk(opener, origin, endpoint, token, stream, metadata, total):
    """续传一块：先问服务端权威偏移，再按该偏移读源文件并 PUT。"""
    current = _call(opener, origin, endpoint, "GET", token)
    offset = current.get("offset") or 0
    if current.get("completed") or offset >= total:
        return current
    length = current.get("chunkBytes")
    if isinstance(length, bool) or not isinstance(length, int) or not 0 < length <= MAX_CHUNK:
        raise RuntimeError(f"服务端返回的分块大小不合法: {length!r}（应在 1..{MAX_CHUNK} 之间）")
    length = min(length, total - offset)
    stream.seek(offset)
    body = stream.read(length)
    if len(body) != length:
        raise RuntimeError("源文件在传输过程中变短了；请冻结交付件后重试")
    return _call(opener, origin, endpoint, "PUT", token, body,
                 {"Content-Type": "application/octet-stream",
                  "Content-Length": str(length), "Upload-Offset": str(offset)})
