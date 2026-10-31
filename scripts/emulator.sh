#!/usr/bin/env bash
# =============================================================================
# scripts/emulator.sh —— 在真机或模拟器上冒烟测试「逐风」APK
#
# 设计前提：全程不需要 root / sudo。
#   * 不修改 /dev/kvm、不改用户组；KVM 不可用时只提示命令由你自己执行，
#     或用 --software 走纯软件模拟（很慢，但能跑）。
#   * 设备来源优先级：--serial 指定  >  已在线的设备（真机/已启动的模拟器）
#     >  自动启动 AVD（--emulator）。
#   * 按设备 ABI 自动挑 APK：x86_64 用 *-x64_*.apk，arm64-v8a 用另一份。
#
# 用法：
#   bash scripts/emulator.sh --emulator               # 起 AVD 测（KVM 可用时）
#   bash scripts/emulator.sh --emulator --software    # 无 KVM 权限，软件模拟
#   bash scripts/emulator.sh                          # 用已连着的真机/模拟器
#   bash scripts/emulator.sh --serial 192.168.1.9:5555
#   bash scripts/emulator.sh --apk /path/to.apk --serial ...
#   bash scripts/emulator.sh --uninstall              # 只卸载
#   bash scripts/emulator.sh --no-site                # 跳过外网站点（baidu）导航测试
#   bash scripts/emulator.sh --site https://example.com   # 换一个外网站点
#   bash scripts/emulator.sh --site-via-input         # 外网站点走 debug 短名（extra_input=baidu）
#
# 可用环境变量覆盖：SDK / AVD / SYSIMG / PKG / ACT / LOGDIR / BOOT_TIMEOUT / RUN_SECONDS / APK
#                   SITE_URL / SITE_VIA_INPUT / SITE_WAIT / SITE_INPUT_ALIAS
# =============================================================================
set -uo pipefail

WORKSPACE=${WORKSPACE:-/home/ural/chromium_android}
# SDK 根解析顺序：显式 SDK > ANDROID_SDK_ROOT > ANDROID_HOME > 约定相邻目录。
# 此前只认 $WORKSPACE/../android-sdk，SDK 装在别处（或换过目录）就只能看到
# "emulator 不存在" 却不知该改哪里。
SDK=${SDK:-${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$WORKSPACE/../android-sdk}}}
APP_DIR=${APP_DIR:-$WORKSPACE/nomad_android}
OUT_DIR=${OUT_DIR:-$APP_DIR/app/build/outputs/apk/debug}

ADB="$SDK/platform-tools/adb"
EMU="$SDK/emulator/emulator"
AVD=${AVD:-nomad-x64}
SYSIMG=${SYSIMG:-$SDK/system-images/android-36/google_apis/x86_64}
PKG=${PKG:-com.nomadbrowser.app}
ACT=${ACT:-.presentation.main.SplashActivity}
LOGDIR=${LOGDIR:-/tmp/apk-test}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-600}      # 等开机秒数（软件模拟请调大，如 1800）
RUN_SECONDS=${RUN_SECONDS:-60}         # App 起来后观察多久

APK=${APK:-}
SERIAL=${SERIAL:-}
WANT_EMU=0
SOFTWARE=0
DO_UNINSTALL=0
KEEP_EMU=0
DO_WEB=1                                # 网页访问冒烟（本地 server + adb reverse）
DO_EXT=1                                # 门面/扩展使用逻辑探针（debug 包专用）
WEB_PORT=${WEB_PORT:-18080}             # 主机本地探针 server 端口（会 adb reverse 进模拟器）
# 外网站点导航（模拟"从外部点开 https://www.baidu.com"）：
#   模拟器通常没有外网，所以**不以外网可达作为通过条件** —— 校验的是"真实域名的导航
#   链路确实被走到"（解析 → 路由 → 加载），再看结果是真渲染还是网络不可达。
DO_SITE=${DO_SITE:-1}
SITE_URL=${SITE_URL:-https://www.baidu.com}
SITE_VIA_INPUT=${SITE_VIA_INPUT:-0}     # 1=走 debug 短名（--es extra_input baidu），0=外部 VIEW
SITE_WAIT=${SITE_WAIT:-15}              # 触发后收结果的等待秒数（DNS/连接失败通常几秒内定论）
SITE_INPUT_ALIAS=${SITE_INPUT_ALIAS:-baidu}  # 走短名时的 dev 别名（见 DEV_URL_SHORT_NAMES）
SITE_LOG=$LOGDIR/site-window.log        # site_smoke 会覆盖为按主机名分文件

log()  { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
err()  { printf '[ERROR] %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --emulator|-e)      WANT_EMU=1 ;;
    --software|-s)      SOFTWARE=1; WANT_EMU=1 ;;
    --serial)           SERIAL=${2:?--serial 需要参数}; shift ;;
    --apk|-a)           APK=${2:?--apk 需要参数}; shift ;;
    --avd)              AVD=${2:?--avd 需要参数}; shift ;;
    --boot-timeout)     BOOT_TIMEOUT=${2:?--boot-timeout 需要参数}; shift ;;
    --run-seconds)      RUN_SECONDS=${2:?--run-seconds 需要参数}; shift ;;
    --uninstall)        DO_UNINSTALL=1 ;;
    --no-web)           DO_WEB=0 ;;
    --no-site)          DO_SITE=0 ;;
    --site)             SITE_URL=${2:?--site 需要参数}; shift ;;
    --site-via-input)   SITE_VIA_INPUT=1 ;;
    --no-ext)           DO_EXT=0 ;;
    --web-port)         WEB_PORT=${2:?--web-port 需要参数}; shift ;;
    --keep-emulator|-k) KEEP_EMU=1 ;;
    --logdir)           LOGDIR=${2:?--logdir 需要参数}; shift ;;
    -h|--help)          sed -n '2,30p' "$0"; exit 0 ;;
    *)                  die "未识别参数: $1（-h 看用法）" ;;
  esac
  shift
done

mkdir -p "$LOGDIR"
[ -x "$ADB" ] || die "adb 不存在: $ADB"

# ── 设备选择 ────────────────────────────────────────────────────────────────
DEV=()                                    # adb -s 参数（可能为空）
online() { $ADB "${DEV[@]}" get-state 2>/dev/null | tr -d '\r' | grep -qx device; }

if [ -n "$SERIAL" ]; then
  DEV=(-s "$SERIAL")
  log "指定设备: $SERIAL"
elif [ "$WANT_EMU" -eq 0 ]; then
  mapfile -t devs < <($ADB devices 2>/dev/null | awk 'NR>1 && $2=="device"{print $1}')
  case ${#devs[@]} in
    0) warn "没有在线设备，转为自动启动 AVD"; WANT_EMU=1 ;;
    1) DEV=(-s "${devs[0]}"); log "使用已在线设备: ${devs[0]}" ;;
    *) die "有多台设备，请用 --serial 指定: ${devs[*]}" ;;
  esac
fi

# ── 启动模拟器（KVM 可用优先；否则软件模拟） ────────────────────────────────
if [ "$WANT_EMU" -eq 1 ]; then
  # 没有 `build.sh android down` 这种动作（build.py 只有 gen/build/package/publish），
  # 所以这里给的是真能照做的命令。
  [ -x "$EMU" ] || die "emulator 不存在: $EMU（装: '$SDK/cmdline-tools/latest/bin/sdkmanager' --install emulator 'system-images;android-36;google_apis;x86_64'；若 SDK 在别处: SDK=<SDK 根> 再跑本脚本）"
  [ -d "$SYSIMG" ] || die "系统镜像不存在: $SYSIMG（同上 sdkmanager 装 system-images;android-36;google_apis;x86_64；或 SYSIMG=<镜像目录> 指定已有的）"

  if [ "$SOFTWARE" -eq 0 ] && { [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; }; then
    cat <<'TIP'
[WARN] 当前用户对 /dev/kvm 无读写权限，KVM 硬件加速不可用。
       本脚本不会替你改系统（无 sudo）。请二选一：

  A. 自己在终端执行一次（要输密码），然后重跑本脚本：
       sudo setfacl -m u:$USER:rw /dev/kvm            # 临时，重启失效
     sudo usermod -aG kvm $USER                       # 永久，需重新登录

  B. 直接跑纯软件模拟（无需任何权限，但极慢，建议 --boot-timeout 1800）：
       bash scripts/emulator.sh --emulator --software --boot-timeout 1800
TIP
    die "KVM 不可用（如需软件模拟请加 --software）"
  fi

  if [ "$SOFTWARE" -eq 1 ]; then
    warn "软件模拟模式（-accel off）：开机可能要十几分钟，请耐心"
    ACCEL=(-accel off)
  else
    ACCEL=(-accel on)
  fi

  log "启动模拟器: $AVD  (日志: $LOGDIR/emulator.log)"
  nohup "$EMU" -avd "$AVD" -sysdir "$SYSIMG" \
        -no-window -no-audio -no-boot-anim -gpu swiftshader \
        -memory 6144 -cores 8 -partition-size 4096 "${ACCEL[@]}" \
        > "$LOGDIR/emulator.log" 2>&1 &
  EMU_PID=$!
  DEV=(-s emulator-5554)

  log "等待设备上线（最多 ${BOOT_TIMEOUT}s）…"
  deadline=$((SECONDS + BOOT_TIMEOUT))
  until online; do
    [ $SECONDS -ge $deadline ] && { tail -20 "$LOGDIR/emulator.log" >&2; die "设备未上线，见 $LOGDIR/emulator.log"; }
    sleep 3
  done
  log "设备已上线 (pid $EMU_PID)"
fi

online || die "没有可用设备"

# ── 等开机完成 ──────────────────────────────────────────────────────────────
log "等待 sys.boot_completed …"
deadline=$((SECONDS + BOOT_TIMEOUT))
until [ "$($ADB "${DEV[@]}" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
  [ $SECONDS -ge $deadline ] && die "开机超时（${BOOT_TIMEOUT}s）。软件模拟请加大 --boot-timeout"
  sleep 5
done
ANDROID_VER=$($ADB "${DEV[@]}" shell getprop ro.build.version.release | tr -d '\r')
ABI=$($ADB "${DEV[@]}" shell getprop ro.product.cpu.abi | tr -d '\r')
RAM=$($ADB "${DEV[@]}" shell getprop ro.build.display.id | tr -d '\r')
log "设备就绪: Android $ANDROID_VER / ABI $ABI / $RAM"

# ── 按 ABI 挑 APK ───────────────────────────────────────────────────────────
if [ -z "$APK" ]; then
  case "$ABI" in
    x86_64)   pat='*-x64_*.apk' ;;
    arm64*)   pat='*.apk';      ;;
    *)        die "未知 ABI: $ABI" ;;
  esac
  # shellcheck disable=SC2207
  cands=($(find "$OUT_DIR" -maxdepth 1 -name "$pat" -type f -printf '%T@ %p\n' 2>/dev/null |
           sort -rn | awk '{ $1=""; sub(/^ /,""); print }'))
  if [ "$ABI" = "x86_64" ]; then
    APK=${cands[0]:-}
  else
    # arm64：排除带 -x64_ 的包
    for c in "${cands[@]}"; do case "$c" in *-x64_*) ;; *) APK=$c; break ;; esac; done
  fi
fi
[ -n "$APK" ] && [ -f "$APK" ] || die "找不到匹配 ABI($ABI) 的 APK（目录: $OUT_DIR）。x86_64 需 *-x64_*.apk，arm64 需先 bash build.sh android build --os android --arch arm64"
log "待装 APK: $(basename "$APK")  ($(du -h "$APK" | cut -f1))"

# ── 卸载（可选） ────────────────────────────────────────────────────────────
if [ "$DO_UNINSTALL" -eq 1 ]; then
  $ADB "${DEV[@]}" uninstall "$PKG" 2>&1 | sed 's/^/[INFO] /'
  exit 0
fi

# ── 安装 ────────────────────────────────────────────────────────────────────
log "安装中（大包，1–3 分钟）…"
install_out=$($ADB "${DEV[@]}" install -r "$APK" 2>&1)
printf '%s\n' "$install_out" | sed 's/^/       /'
case "$install_out" in
  *Success*) ;;
  *NO_MATCHING_ABIS*)       die "ABI 不匹配：设备 $ABI，APK 只有 x86_64。请改用 arm64 包" ;;
  *INSUFFICIENT_STORAGE*)   die "存储不足：重建 AVD 加大 -partition-size，或真机清理空间" ;;
  *UPDATE_INCOMPATIBLE*)    die "签名/数据冲突：先跑本脚本 --uninstall 再测" ;;
  *)                        die "安装失败" ;;
esac
$ADB "${DEV[@]}" shell dumpsys package "$PKG" 2>/dev/null |
  grep -E "versionName|primaryCpuAbi" | sed 's/^/[INFO] /'

# ── 启动 + 抓日志 ───────────────────────────────────────────────────────────
BOOT_LOG=$LOGDIR/boot.log
$ADB "${DEV[@]}" logcat -c
$ADB "${DEV[@]}" logcat -v time > "$BOOT_LOG" 2>&1 &
LC_PID=$!
log "启动 $PKG$ACT"
$ADB "${DEV[@]}" shell am start -n "$PKG/$ACT" -W 2>&1 | grep -E "Status|ThisTime|TotalTime|Error" | sed 's/^/       /'
# ── 观察期内的两段主动冒烟 ──────────────────────────────────────────────────
# 1) 网页访问：模拟器没有外网，所以用「主机 python3 http.server + adb reverse」
#    提供 http://127.0.0.1:<port>/ —— 走的是内核真实网络栈。
#    判定看服务端 access log：命中 /probe.html = 内核确实发了请求；
#    再命中 /probe-js = 页面 JS 真的执行了（同源 fetch 回环）。
# 2) 扩展/门面使用逻辑：触发 debug-only 探针 Receiver（co.arupa.* 门面调用：
#    contextMenus 注册表 / skills 中继 / 扩展存储 / 扩展注册表），逐项取回结果。
web_smoke() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "跳过  python3 不可用，起不了本地探针 server"
    return 0
  fi
  local dir="$LOGDIR/webroot" logf="$LOGDIR/webserver.log"
  rm -rf "$dir"; mkdir -p "$dir"
  cat > "$dir/probe.html" <<'HTML'
<!doctype html><html><head><meta charset="utf-8"><title>nomad-probe</title>
<style>body{font:14px/1.5 monospace}pre{white-space:pre-wrap;word-break:break-all}</style></head>
<body><h1>nomad smoke probe</h1>
<!-- 页面直接把 UA 显示出来：模拟器屏幕上就能肉眼核对"这颗内核对外报的是什么" -->
<pre id="ua">UA: (loading)</pre>
<pre id="uach">UA-CH: (loading)</pre>
<script>
  var ua = navigator.userAgent;
  document.getElementById('ua').textContent = 'UA: ' + ua;
  var d = navigator.userAgentData;
  document.getElementById('uach').textContent = 'UA-CH: mobile=' + (d ? d.mobile : 'n/a') +
      ' platform=' + (d ? d.platform : 'n/a') +
      ' brands=' + ((d && d.brands) ? d.brands.length : 0) +
      ' vp=' + window.innerWidth + 'x' + window.innerHeight;
  // 把 JS 侧读到的 UA 回传：与 server 记录的**请求头 UA** 对照，
  // 两者不一致说明内核在请求头层面重写了 UA（反风控/UA-CH 关注的正是这里）。
  // 两个 fetch 都自证：成功打 PROBE_OK（含 status / 字节数），失败打 PROBE_FETCH_FAIL。
  // 若被 App 侧拦截返回「200 空响应」，会看到 status=200 但 bytes=0 ——
  // logcat 的 JS-CONSOLE 直接给出判定，不必再靠"服务端没收到"反推。
  function probeFetch(path, onOk) {
    fetch(path).then(function (r) {
      return r.text().then(function (b) {
        console.log('PROBE_OK ' + path + ' status=' + r.status + ' bytes=' + b.length);
        if (onOk) onOk(r, b);
      });
    }).catch(function (e) {
      console.log('PROBE_FETCH_FAIL ' + path + ' ' + e);
    });
  }
  probeFetch('/probe-ua?js=' + encodeURIComponent(ua));
  // 同源 fetch：只有页面真被内核加载并执行 JS，才会出现第二条请求
  probeFetch('/probe-js?stage=executed', function () { document.title = 'nomad-probe-js-ok'; });
</script>
</body></html>
HTML
  # 用自带的小 server 而不是 python3 -m http.server：后者的 access log 不记录请求头，
  # 拿不到"内核真正发出去的 UA"。这里每行打成 `GET <path> UA=<请求头>`，
  # 并把页面回传的 JS 侧 UA 单独打成 `JS-UA <ua>`，便于两边对照。
  cat > "$LOGDIR/webserver.py" <<'PYEOF'
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

port, root = int(sys.argv[1]), sys.argv[2]


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        ua = self.headers.get('User-Agent', '-')
        sys.stderr.write('GET %s UA=%s\n' % (self.path, ua))
        if self.path.startswith('/probe-ua'):
            q = parse_qs(urlparse(self.path).query)
            if 'js' in q:
                sys.stderr.write('JS-UA %s\n' % unquote(q['js'][0]))
        sys.stderr.flush()


ThreadingHTTPServer(('127.0.0.1', port),
                    functools.partial(Handler, directory=root)).serve_forever()
PYEOF
  python3 "$LOGDIR/webserver.py" "$WEB_PORT" "$dir" > "$logf" 2>&1 &
  local srv=$!
  sleep 1
  $ADB "${DEV[@]}" reverse "tcp:$WEB_PORT" "tcp:$WEB_PORT" >/dev/null 2>&1
  local url="http://127.0.0.1:$WEB_PORT/probe.html"
  $ADB "${DEV[@]}" shell am start -a android.intent.action.VIEW -d "$url" \
      -n "$PKG/com.nomadbrowser.app.presentation.browser.BrowserActivity" >/dev/null 2>&1
  sleep 12
  local hit_page hit_js
  hit_page=$(grep -c "GET /probe.html" "$logf" 2>/dev/null || true)
  hit_js=$(grep -c "GET /probe-js" "$logf" 2>/dev/null || true)
  kill "$srv" 2>/dev/null || true
  wait "$srv" 2>/dev/null || true
  $ADB "${DEV[@]}" reverse --remove "tcp:$WEB_PORT" >/dev/null 2>&1
  if [ "${hit_page:-0}" -gt 0 ] && [ "${hit_js:-0}" -gt 0 ]; then
    echo "PASS  网页导航 + 页面 JS 执行（page=${hit_page} js=${hit_js}）$url"
  elif [ "${hit_page:-0}" -gt 0 ]; then
    echo "WARN  页面已加载但 JS 未回环（page=${hit_page} js=${hit_js}）$url"
  else
    echo "FAIL  内核未发起请求（page=0 js=0）$url"
  fi
  # UA 三处对照（2026-09-29）：① 内核真实请求头 ② 页面 JS 自报 ③ App 侧 UA-CHECK。
  # 只做显示 + 一致性弱判定：请求头 ≠ JS 自报 ⇒ 内核在请求头层面改了 UA，
  # 这正是反风控/UA-CH 要盯的地方；UA 具体该是什么由产品决定，装置不硬编码期望值。
  local ua_hdr ua_js ua_app
  ua_hdr=$(grep -a "GET /probe.html" "$logf" 2>/dev/null | head -1 | sed 's/.*UA=//')
  ua_js=$(grep -a "^JS-UA " "$logf" 2>/dev/null | head -1 | sed 's/^JS-UA //')
  # logcat 行形如 `I/UA-CHECK(12345): {...}` —— tag 后同样隔着 `(pid): `，
  # 只按 "UA-CHECK: " 切会把整行前缀留在结果里（首轮实测就是这样）。
  ua_app=$(grep -a "UA-CHECK" "$BOOT_LOG" 2>/dev/null | tail -1 | sed -E 's/.*UA-CHECK(\([0-9]+\))?: *//')
  {
    echo "内核请求头 UA  : ${ua_hdr:-<未捕获>}"
    echo "页面 JS 自报 UA: ${ua_js:-<未捕获>}"
    echo "App UA-CHECK   : ${ua_app:-<未捕获>}（ua/mob/platform/brands/vp，见 BrowserKernelClientController）"
  } > "$LOGDIR/web-ua.txt"
  if [ -n "$ua_hdr" ] && [ -n "$ua_js" ] && [ "$ua_hdr" = "$ua_js" ]; then
    echo "PASS  网页 UA 一致（请求头 = JS 自报）${ua_hdr}"
  elif [ -z "$ua_js" ]; then
    # 缺的不是"两边不同"，而是页面侧根本没回传（JS 未执行/被拦/页面没走完），
    # 说成"不一致"会误导排查方向。
    echo "WARN  页面未回传 JS 自报 UA（页面 JS 可能未执行或被拦；明细 $LOGDIR/web-ua.txt）"
  elif [ -n "$ua_hdr$ua_js" ]; then
    echo "WARN  网页 UA 不一致（请求头='$ua_hdr' / JS 自报='$ua_js'；明细 $LOGDIR/web-ua.txt）"
  else
    echo "WARN  未捕获到网页 UA（明细 $LOGDIR/web-ua.txt）"
  fi
}

# 3) 外网站点导航：模拟"从外部点开 https://www.baidu.com"这类真实站点。
#    模拟器通常没有外网（见文件头"无外网也能测"），因此**不以外网可达作为通过条件**，
#    而是校验"真实域名的导航链路确实被走到"，再看落点：
#      · 入口三段（App 侧日志都带 destinationHost）：NAV_RESOLVED / ROUTE_DECIDED /
#        LOAD_URL_CALL 必须都命中该 host —— 缺则说明入口/路由/加载被吞，判 FAIL；
#      · 结果：① 出现 PAGE-CHECK 且标题不是 URL 本身 ⇒ 页面真渲染（有外网）；
#              ② 只有 net::ERR_* / webNavigation.onErrorOccurred ⇒ 无外网/被代理拦，
#                导航链路已走通、如期失败（**这是预期结果，算 PASS**）；
#              ③ 两者都没有 ⇒ WARN（超时/SW 拦截，明细留在 $SITE_LOG）。
#    崩溃不在这里判（由报告第 2 节统一覆盖），避免同一件事两处结论不一致。
site_smoke() {
  local url="$SITE_URL" host
  host=$(printf '%s' "$url" | sed -E 's#^[a-z]+://([^/]+).*#\1#')
  if [ -z "$host" ]; then
    echo "FAIL  站点 URL 不合法: $url"
    return 0
  fi
  SITE_LOG="$LOGDIR/site-${host}.log"
  local mark
  mark=$(wc -l < "$BOOT_LOG" 2>/dev/null || echo 0)
  if [ "$SITE_VIA_INPUT" -eq 1 ]; then
    # debug 包的 dev 短名路径：extra_input=baidu → https://www.baidu.com
    # （DEV_URL_SHORT_NAMES 仅在 BuildConfig.DEBUG 下生效；release 包请用外部 VIEW 路径）
    $ADB "${DEV[@]}" shell am start -W -n "$PKG/com.nomadbrowser.app.presentation.browser.BrowserActivity" \
        --es extra_input "$SITE_INPUT_ALIAS" >/dev/null 2>&1
  else
    $ADB "${DEV[@]}" shell am start -W -a android.intent.action.VIEW -d "$url" \
        -n "$PKG/com.nomadbrowser.app.presentation.browser.BrowserActivity" >/dev/null 2>&1
  fi
  sleep "$SITE_WAIT"
  # 只截取本次触发之后的日志窗口：避免把启动期噪声（如 Cronet 探测的
  # ERR_CONNECTION_REFUSED、上一次本地 probe 的 PAGE-CHECK）算进本段结论。
  awk -v n="$mark" 'NR > n' "$BOOT_LOG" > "$SITE_LOG" 2>/dev/null || : > "$SITE_LOG"
  local h_nav h_route h_load page title errs
  h_nav=$(grep -aF "NAV_RESOLVED" "$SITE_LOG" 2>/dev/null | grep -acF "destinationHost='$host'" || true)
  h_route=$(grep -aF "ROUTE_DECIDED" "$SITE_LOG" 2>/dev/null | grep -acF "destinationHost='$host'" || true)
  h_load=$(grep -aF "LOAD_URL_CALL" "$SITE_LOG" 2>/dev/null | grep -acF "destinationHost='$host'" || true)
  # 注意 logcat 行格式是 `I/PAGE-CHECK(pid): url=… title='…'`：tag 与 url 之间隔着
  # `(pid): `，不能用 "PAGE-CHECK url=" 一次匹配（曾因此把真渲染判成 WARN）。
  page=$(grep -aF "PAGE-CHECK" "$SITE_LOG" 2>/dev/null | grep -aF "url=$url" | head -1)
  title=""
  [ -n "$page" ] && title=$(printf '%s' "$page" | sed -E "s/.*title='([^']*)'.*/\1/")
  errs=$(grep -aoE "net::ERR_[A-Z_]+|ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED|ERR_PROXY_CONNECTION_FAILED|webNavigation\.onErrorOccurred" "$SITE_LOG" 2>/dev/null | sort -u | tr '\n' ' ')
  # 主框架是否已提交/完成：onCompleted / PAGE-CHECK 出现即说明主框架走完过。
  # 有它时 onErrorOccurred、net::ERR_ 多来自子资源（baidu 那次 onCompleted 44.053 先到、
  # onErrorOccurred 44.142 后到，站点其实完整渲染），不能据此判"站点不可达"。
  committed=$(grep -acE "onCompleted|PAGE-CHECK" "$SITE_LOG" 2>/dev/null || true)
  if [ "${h_nav:-0}${h_route:-0}${h_load:-0}" = "000" ]; then
    echo "FAIL  入口未走到 $host（解析/路由/加载三段日志都没有该 host；明细 $SITE_LOG）"
    return 0
  fi
  if [ -n "$page" ] && [ -n "$title" ] && [ "$title" != "$url" ] && [ "$title" != "$url/" ]; then
    echo "PASS  外网站点已打开（$host 标题='$title'；解析/路由/加载=${h_nav}/${h_route}/${h_load}）"
  elif [ "${committed:-0}" -gt 0 ]; then
    # 主框架已提交：即使有 net::ERR_/onErrorOccurred，也是子资源级失败，站点本身可达。
    echo "PASS  外网站点导航链路已走通、主框架已提交（$host 解析/路由/加载=${h_nav}/${h_route}/${h_load}；${errs:+已上报：${errs% }}）"
  elif [ -n "$errs" ]; then
    echo "WARN  外网站点导航链路已走通、主框架未提交且报了网络错误（$host 解析/路由/加载=${h_nav}/${h_route}/${h_load}；${errs% }）"
  elif [ -n "$page" ]; then
    echo "WARN  外网站点加载已结束但无正式标题（$host title='$title'；可能是错误页/空标题）"
  else
    echo "WARN  ${SITE_WAIT}s 内既无提交也无网络错误（$host；明细 $SITE_LOG）"
  fi
}

ext_probe() {
  # 防假 PASS：探针结果是 app 私有目录里的一个文件，而 `adb install -r` 以及复用
  # 的 AVD data 分区都会把上一次安装（甚至上一个 checkout 的调试包）留下的
  # files/smoke-probe.txt 原样保留 —— 不先删就会把旧结果当本次结果报 PASS。
  $ADB "${DEV[@]}" shell run-as "$PKG" rm -f files/smoke-probe.txt >/dev/null 2>&1

  # 只在本 APK 确实装了接收器时才把"没结果"算失败；否则是探针代码不在这份包里
  # （App 仓已无 SmokeProbeReceiver.kt，只有早期 checkout 的调试包才有）。
  # unzip 不可用时保守视为"可能有"，避免误判 SKIP。
  local recv_absent=0
  if command -v unzip >/dev/null 2>&1; then
    unzip -p "$APK" 'classes*.dex' 2>/dev/null | grep -aq SmokeProbeReceiver || recv_absent=1
  fi

  $ADB "${DEV[@]}" shell am broadcast \
      -a com.nomadbrowser.app.DEBUG_SMOKE_PROBE \
      -n "$PKG/com.nomadbrowser.app.debug.SmokeProbeReceiver" \
      --es scenario all >/dev/null 2>&1
  sleep 10
  local probe
  probe=$($ADB "${DEV[@]}" shell run-as "$PKG" cat files/smoke-probe.txt 2>/dev/null | tr -d '\r')
  if [ -z "$probe" ]; then
    # run-as 不可用或文件未落盘：退回 logcat（探针同时打 tag=ArupaSmokeProbe）
    probe=$(grep -a "ArupaSmokeProbe" "$BOOT_LOG" | sed 's/.*ArupaSmokeProbe: //' | tr -d '\r')
  fi
  printf '%s\n' "$probe" > "$LOGDIR/smoke-probe.txt"
  local sum
  sum=$(printf '%s\n' "$probe" | grep -o "PROBE-SUMMARY pass=[0-9]* fail=[0-9]*" | tail -1)
  if [ -z "$sum" ]; then
    if [ "$recv_absent" -eq 1 ]; then
      echo "SKIP  门面未被验证：本 APK 未包含 $PKG.debug.SmokeProbeReceiver（源码已不在 App 仓；本节无覆盖，探针结论不作数）"
    else
      echo "FAIL  探针未产出结果（Receiver 未触发 / 内核未就绪；明细见 $LOGDIR/smoke-probe.txt）"
    fi
  elif printf '%s' "$sum" | grep -qE "fail=0$"; then
    echo "PASS  $sum"
  else
    echo "FAIL  $sum（明细见 $LOGDIR/smoke-probe.txt）"
  fi
}

log "观察 ${RUN_SECONDS}s …"
SETTLE=$(( RUN_SECONDS / 3 ))
[ "$SETTLE" -lt 8 ] && SETTLE=8
sleep "$SETTLE"

WEB_RESULT="跳过（--no-web）"
if [ "$DO_WEB" -eq 1 ]; then
  log "网页访问：主机本地 server + adb reverse（模拟器无外网也能测）"
  WEB_RESULT=$(web_smoke)
  log "  $WEB_RESULT"
fi

SITE_RESULT="跳过（--no-site）"
if [ "$DO_SITE" -eq 1 ]; then
  if [ "$SITE_VIA_INPUT" -eq 1 ]; then
    log "外网站点导航：模拟 dev 短名打开「$SITE_INPUT_ALIAS」（目标 $SITE_URL）"
  else
    log "外网站点导航：模拟从外部点开 $SITE_URL"
  fi
  SITE_RESULT=$(site_smoke)
  log "  $(printf '%s\n' "$SITE_RESULT" | head -1)"
fi

EXT_RESULT="跳过（--no-ext）"
if [ "$DO_EXT" -eq 1 ]; then
  log "门面使用逻辑：触发 debug 探针（contextMenus / skills / storage / activeTab / 扩展状态 / 宿主 / 代理 / 身份 / 内核 / MV3）"
  EXT_RESULT=$(ext_probe)
  log "  $EXT_RESULT"
fi

sleep 5
kill $LC_PID 2>/dev/null
wait $LC_PID 2>/dev/null

# ── 判定 ────────────────────────────────────────────────────────────────────
REPORT=$LOGDIR/report.txt
{
  echo "===== 冒烟测试报告 $(date '+%F %T') ====="
  echo "设备  : $($ADB "${DEV[@]}" shell getprop ro.product.model | tr -d '\r') / Android $ANDROID_VER / $ABI"
  echo "APK   : $APK"
  echo
  echo "--- 1. 进程 ---"
  pid=$($ADB "${DEV[@]}" shell pidof "$PKG" | tr -d '\r')
  [ -n "$pid" ] && echo "PASS  进程存活 pid=$pid" || echo "FAIL  进程不存在"
  echo
  echo "--- 2. 崩溃 / ANR ---"
  # has died 只认被测包：系统进程（GMS 的 safetyhub / cch+CEM 缓存回收等）死掉与被测
  # App 无关，全量匹配会把每次运行都判成 FAIL（2026-09-26 实测噪声源）。
  crash_res=$(grep -nE "FATAL EXCEPTION|AndroidRuntime: (FATAL|Process)|am_anr|SIGSEGV|SIGABRT|signal 6" "$BOOT_LOG")
  pkg_died=$(grep -nE "Process ${PKG//./\\.} .*(has died|has crashed|ANR)" "$BOOT_LOG")
  if [ -n "$crash_res$pkg_died" ]; then
    echo "FAIL  发现崩溃/ANR："
    printf '%s\n%s\n' "$crash_res" "$pkg_died" | grep -v '^$' | head -20
  else
    echo "PASS  日志中无 FATAL / ANR"
  fi
  ts=$($ADB "${DEV[@]}" shell "ls -l /data/tombstones 2>/dev/null | tail -3" | tr -d '\r')
  # 设备上的 tombstone 可能是历史遗留（模拟器 AVD 数据不随重启清空）——给时间戳自行判断。
  [ -n "$ts" ] && printf '注意  设备 tombstone 目录（可能含历史，请按时间判断）:\n%s\n' "$ts"
  echo
  echo "--- 3. 内核加载（so / pak / icudtl / V8 / 插件宿主）---"
  khits=$(grep -icE "libarupakernel|arupa_kernel\.pak|icudtl|v8_context|snapshot_blob|arupa_kernel_resources|libarupapluginhost\.so|nomad-plugin-runtime" "$BOOT_LOG")
  echo "命中行数: $khits"
  grep -inE "libarupakernel|arupa_kernel\.pak|icudtl|v8_context|snapshot_blob|arupa_kernel_resources|libarupapluginhost\.so|nomad-plugin-runtime" "$BOOT_LOG" | head -25
  echo
  echo "--- 4. 前台 Activity ---"
  $ADB "${DEV[@]}" shell dumpsys activity activities 2>/dev/null | grep -E "mResumedActivity|topResumedActivity" | head -3
  echo
  echo "--- 5. 错误关键字 ---"
  # 只统计被测包各进程（主进程 + renderer/sandbox）的日志行。全量匹配会把他包噪声当成
  # 被测 App 的问题（2026-10-12 实测：PrebuiltBugle 缺 libcurve25519.so 被误收）。
  # logcat -v time 行格式 `MM-DD HH:MM:SS.mmm  PID  TID L TAG: msg` → $3 即 pid。
  app_pids=$($ADB "${DEV[@]}" shell "ps -A 2>/dev/null" 2>/dev/null | tr -d '\r' \
      | awk -v p="$PKG" 'index($0, p) {print $1}' | sort -u | paste -sd'|' -)
  if [ -n "$app_pids" ]; then
    grep -aE "ERR_|net::|ClassNotFound|NoSuchMethod|UnsatisfiedLink|dlopen failed|Permission denied" "$BOOT_LOG" \
      | awk -v pids="$app_pids" 'BEGIN{n=split(pids,a,"|");for(i=1;i<=n;i++)w[a[i]]=1} $3 in w {print NR": "$0}' \
      | head -15
  else
    echo "（ps 未取到包进程，无法按 pid 过滤；以下为全量匹配，可能含他包噪声）"
    grep -inE "ERR_|net::|ClassNotFound|NoSuchMethod|UnsatisfiedLink|dlopen failed|Permission denied" "$BOOT_LOG" | head -15
  fi
  echo
  echo "--- 6. 内存 ---"
  # 只 head -8 会截到 Native Heap 就停，判不了整体内存；改抓 App Summary 与 TOTAL 关键行。
  $ADB "${DEV[@]}" shell dumpsys meminfo "$PKG" 2>/dev/null \
    | grep -E "App Summary|Java Heap|Native Heap|Code|Stack|Graphics|Private Other|System|TOTAL" \
    | head -20
  echo
  echo "--- 7. 网页访问（本地 server + adb reverse / 外网站点）---"
  echo "${WEB_RESULT:-跳过}"
  if [ -f "$LOGDIR/webserver.log" ]; then
    echo "服务端 access log（末 8 行，含请求头 UA）:"
    tail -8 "$LOGDIR/webserver.log" | cut -c1-160 | sed 's/^/  /'
  fi
  if [ -s "$LOGDIR/web-ua.txt" ]; then
    echo "网页 UA 三处对照:"
    sed 's/^/  /' "$LOGDIR/web-ua.txt"
  fi
  # 探针页自证：PROBE_OK（status/bytes）或 PROBE_FETCH_FAIL 直接判回环是否被吃。
  js_probe=$(grep -a "PROBE_OK\|PROBE_FETCH_FAIL" "$BOOT_LOG" 2>/dev/null | tail -6 | cut -c1-160)
  if [ -n "$js_probe" ]; then
    echo "页面 fetch 自证（JS-CONSOLE）:"
    printf '%s\n' "$js_probe" | sed 's/^/  /'
  fi
  echo "${SITE_RESULT:-跳过}"
  if [ -s "${SITE_LOG:-}" ]; then
    echo "外网站点窗口内的关键日志:"
    grep -aE "NAV_RESOLVED|ROUTE_DECIDED|LOAD_URL_CALL|PAGE-CHECK|UA-CHECK|onErrorOccurred|net::ERR_" \
        "$SITE_LOG" 2>/dev/null | head -8 | cut -c1-160 | sed 's/^/  /'
  fi
  echo
  echo "--- 8. 扩展 / 门面使用逻辑 ---"
  echo "${EXT_RESULT:-跳过}"
  if [ -s "$LOGDIR/smoke-probe.txt" ]; then
    echo "探针逐项结果:"
    sed 's/^/  /' "$LOGDIR/smoke-probe.txt"
  fi
  echo
  echo "--- 9. 产物一致性（APK 里的 so 是否就是本次交付）---"
  # 教训（2026-09-27）：migrate.sh 曾因 .delivery-build 里是 …-32.zip 而静默失败，
  # AAR/APK 用的还是旧 .so —— 而冒烟"全 PASS"，掩盖了"新修复没进包"。这一节把
  # APK / AAR / 最新交付目录三处的内核 .so BuildId 摆在一起，不一致就 WARN。
  _WS="${APK%%/nomad_android/*}"
  _APP="${APK%%/build/outputs/apk/*}"
  python3 - "$APK" "$_APP" "$_WS" <<'PYEOF' 2>/dev/null | sed 's/^/      /'
import sys, zipfile, pathlib

apk, app, ws = sys.argv[1], sys.argv[2], sys.argv[3]


def build_id(data):
    i = data.find(b'GNU\x00')
    return data[i + 4:i + 24].hex() if i >= 0 else None


res = {}
for key, path, entry in (
        ('APK', apk, 'lib/x86_64/libarupakernel.so'),
        ('AAR', str(pathlib.Path(app) / 'libs/kernel/x64/arupa-kernel.aar'),
         'jni/x86_64/libarupakernel.so')):
    try:
        with zipfile.ZipFile(path) as z:
            res[key] = build_id(z.read(entry))
    except Exception:
        res[key] = None

# 交付侧按 BuildId **匹配**（不是取排序最后那个目录）：本地会同时存在多个含同一 .so 的
# 目录（-1/-2 单架构包、-N 多 ABI 合并包）。此前取 dirs[-1] 会在用合并包时误报 WARN
# （2026-09-28 实际发生：APK/AAR 都是 8f85…，只因 -2 排在最后而被判不一致）。
want = {res.get('APK'), res.get('AAR')} - {None}
found = None
scanned = []
dirs = sorted(p for p in (pathlib.Path(ws) / 'dist').glob('kernel-android-*')
              if p.is_dir())
for d in dirs:
    for cand in (d / 'kernel/x64/libarupa_kernel.so', d / 'x64/libarupa_kernel.so'):
        if not cand.is_file():
            continue
        got = build_id(cand.read_bytes())
        scanned.append(f'{d.name}={got[:12] if got else "无"}')
        if got in want and not found:
            found = (d.name, got)
        break
if scanned:
    print('dist 交付候选 : ' + ', '.join(scanned))
res['交付'], res['交付目录'] = ((found[1], found[0]) if found else (None, '(未匹配)'))

for k in ('APK', 'AAR', '交付'):
    print(f'{k:4s} so BuildId : {res.get(k)}')
print(f'交付目录     : {res.get("交付目录", "(none)")}')
vals = {res.get(k) for k in ('APK', 'AAR', '交付')}
if found is None:
    # 没有交付目录时是"没得比"，而非"不一致" —— 单独提示，避免被读成 APK 不是最新。
    print('结论         : WARN  交付目录未匹配 —— dist/ 下没有与 APK/AAR 同 BuildId 的交付包'
          '（先跑 migrate.sh 产出交付目录；缺交付目录时无法核对 APK 是否最新）')
elif len(vals) == 1 and None not in vals:
    print('结论         : PASS  APK / AAR / 交付 同源')
else:
    print('结论         : WARN  不一致 —— APK 可能不是最新交付'
          '（先 migrate.sh + build.sh，或检查 .delivery-build）')
PYEOF
  echo
  echo "--- 10. 探针覆盖对照（门面公开 API 有没有被探到）---"
  # 探针盲区是上一轮"签名漂移躲过所有检查"的根源（revokeActiveTabTab 没被调过）。
  # 这里把门面各桥的 public static 方法与探针源码里实际调用的名字做差集。
  python3 - "$_APP" "$_WS" <<'PYEOF' 2>/dev/null | sed 's/^/      /'
import os, sys, zipfile, pathlib, re, subprocess, tempfile

app, ws = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
aar = app / 'libs/kernel/x64/arupa-kernel.aar'
# 探针源码不入库、位置会变：允许用 PROBE_SRC 指定实际路径
kt = pathlib.Path(os.environ.get('PROBE_SRC')
                  or (app / 'src/debug/java/com/nomadbrowser/app/debug/SmokeProbeReceiver.kt'))
jdk = ws / 'src/third_party/jdk/current/bin/javap'
try:
    with zipfile.ZipFile(aar) as z:
        cj = pathlib.Path(tempfile.mkdtemp()) / 'c.jar'
        cj.write_bytes(z.read('classes.jar'))
except Exception as e:
    print('跳过：读不到 classes.jar（%s）' % e)
    raise SystemExit

classes = [
    'co.arupa.plugin.ArupaPluginBridge', 'co.arupa.proxy.ArupaProxyBridge',
    'co.arupa.identity.ArupaIdentityBridge',
    'co.arupa.license.ArupaLicenseBridge', 'co.arupa.kernel.ArupaKernel',
    'co.arupa.kernel.DevToolsBackend', 'co.arupa.mv3.ArupaMv3Guard',
    'co.arupa.mv3.ArupaPluginStorage',
]
try:
    r = subprocess.run([str(jdk), '-p', '-cp', str(cj), *classes],
                       capture_output=True, text=True)
except Exception as e:
    print('跳过：javap 不可用（%s）' % e)
    raise SystemExit

api = set()
for line in r.stdout.splitlines():
    s = line.strip()
    if s.startswith('public static') and '(' in s and '=' not in s:
        m = re.match(r'^public static [\w<>,\.\[\]\$ ]*?\s(\w+)\(', s)
        if m:
            api.add(m.group(1))

if not kt.is_file():
    # 探针源码不在时"已覆盖"必然为 0，直接说明测量不可用 —— 避免被读成"覆盖退化"
    # （2026-09-28 的报告里就这样被误读过一次）。
    print(f'门面公开方法 {len(api)} 个；门面未被验证：覆盖统计不可用（探针源码不存在）')
    print(f'  {kt}')
    print('  可用 PROBE_SRC=/path/to/SmokeProbeReceiver.kt 指定实际路径')
    raise SystemExit

called = set()
if kt.is_file():
    txt = kt.read_text(encoding='utf-8', errors='replace')
    # 门面桥类名：Arupa* 与 DevToolsBackend（此前漏了后者，把 attach 误报成未覆盖）；
    # 尾随实参可能是括号也可能是 lambda 的 `{`（如 DevToolsBackend.attach { _ -> }）。
    called |= set(re.findall(r'\b(?:Arupa\w+|DevToolsBackend)\.(\w+)\s*[({]', txt))

# 探针不宜/无法直接调用的三类（如实列出而不是混在"未覆盖"里）：
#   · native 侧自己调的生命周期入口；
#   · 需要真实 WebContents/视图的（探针没有视图句柄）；
#   · 有进程级副作用的（起停隔离引擎）。
NOT_PROBEABLE = {
    'initialize', 'loadNativeLib', 'ensureResourceContextForCurrentProcess',
    'setExtensionCookiePartition', 'setTabIdForWebContents', 'setPacScript',
    'setExtensionRoot', 'setPluginRoot',
    'setPacScriptForExtension', 'setTransportRoute',
    'setTransportRouteForExtension', 'getWebRtcIpPolicy', 'setIdentityForTab',
    'clearIdentityForTab', 'startIsolatedEngine', 'stopIsolatedEngine',
}

missing = sorted(api - called - NOT_PROBEABLE)
skipped = sorted((api & NOT_PROBEABLE))
# `called` 是"探针文件里出现过的 Arupa*/DevToolsBackend.* 方法名"，**不保证是 api 的
# 子集**：App 侧同前缀的类（ArupaKernelInit 等）以及门面仓里没有对应 .java 壳的方法
# 都会落进来。此前直接把 len(called) 当分子打印，出现过 "91 调用 + 16 不适用 = 107 >
# 106 公开方法"这种加起来不等于分母的报告（2026-09-28 复盘）。按集合关系重打：
probeable = sorted(api - NOT_PROBEABLE)
covered = sorted(set(probeable) - set(missing))
outside = sorted(called - api)
print(f'门面公开方法 {len(api)} 个（可探针 {len(probeable)} = 已覆盖 {len(covered)}'
      f' + 未覆盖 {len(missing)}）')
if missing:
    print(f'未被覆盖 {len(missing)} 个: ' + ', '.join(missing))
else:
    print('未覆盖: 无')
if outside:
    print(f'另有 {len(outside)} 个被调用名不属于上述门面公开 API（不计入覆盖分母）: '
          + ', '.join(outside))
if skipped:
    print(f'不适用探针 {len(skipped)} 个（需视图/native/有副作用）: '
          + ', '.join(skipped))
PYEOF
} > "$REPORT" 2>&1

# 截图取证
$ADB "${DEV[@]}" shell screencap -p /sdcard/smoke.png >/dev/null 2>&1 \
  && $ADB "${DEV[@]}" pull /sdcard/smoke.png "$LOGDIR/screen.png" >/dev/null 2>&1 \
  && log "截图: $LOGDIR/screen.png"

cat "$REPORT"
echo
log "完整日志: $BOOT_LOG"
log "报告    : $REPORT"

if [ "$KEEP_EMU" -eq 0 ] && [ "$WANT_EMU" -eq 1 ] && [ -n "${EMU_PID:-}" ]; then
  log "关闭模拟器"
  $ADB "${DEV[@]}" emu kill >/dev/null 2>&1 || kill "$EMU_PID" 2>/dev/null
fi
