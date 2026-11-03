#!/usr/bin/env bash
set -euo pipefail

payload=''
probe=''
plugin=''
egress=''
timeout_seconds=420
verify_connections=0
while (($#)); do
  case "$1" in
    --payload-root) payload=$2; shift 2 ;;
    --probe-root) probe=$2; shift 2 ;;
    --plugin-zip) plugin=$2; shift 2 ;;
    --egress-url) egress=$2; shift 2 ;;
    --timeout-seconds) timeout_seconds=$2; shift 2 ;;
    --verify-connections) verify_connections=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -d "$payload" && -n "$probe" && -f "$plugin" ]] || {
  echo 'Usage: probe-mivpn-native.sh --payload-root DIR --probe-root NEW_DIR --plugin-zip ZIP [--verify-connections --egress-url HTTPS_URL]' >&2
  exit 2
}
payload=$(cd "$payload" && pwd -P)
plugin=$(cd "$(dirname "$plugin")" && printf '%s/%s' "$PWD" "$(basename "$plugin")")
[[ ! -e "$probe" ]] || { echo 'Probe root must be a new disposable directory.' >&2; exit 2; }
probe_parent=$(cd "$(dirname "$probe")" && pwd -P)
probe="$probe_parent/$(basename "$probe")"

app=$(find "$payload" -maxdepth 1 -type d -name '*.app' -print -quit)
if [[ -n "$app" ]]; then
  executable="$app/Contents/MacOS/NomadBrowser"
  arupa="$app/Contents/Resources/arupa-mac"
  # Run the managed entry point for an isolated probe. LaunchServices rejects the
  # ad-hoc-signed development bundle and may route a matching bundle identifier
  # to an already-running production instance.
  launch=(dotnet "$app/Contents/MacOS/NomadBrowser.dll")
else
  executable="$payload/NomadBrowser"
  arupa="$payload/arupa-desktop"
  [[ -d "$arupa" ]] || arupa="$payload/kernel"
  launch=("$executable")
fi
[[ -x "$executable" && -d "$arupa" ]] || { echo 'Debug executable or Arupa delivery is missing.' >&2; exit 2; }

python3 - "$payload" "$plugin" "$egress" "$verify_connections" <<'PY'
import ipaddress, json, pathlib, sys, urllib.parse, zipfile
payload, plugin, endpoint, verify = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4] == '1'
manifests = list(payload.glob('build-manifest.json'))
if manifests:
    identity = json.loads(manifests[0].read_text())['identity']
    if identity.get('project') != 'nomad_desktop' or identity.get('variant') != 'debug':
        raise SystemExit('MiVPN probe requires a sealed Debug desktop delivery.')
expected = {'manifest.json','background.js','popup.html','assets/popup.css','assets/popup.js','offline-nodes.json'}
with zipfile.ZipFile(plugin) as archive:
    names = {row.filename for row in archive.infolist() if not row.is_dir()}
    if names != expected or any(row.filename.startswith('/') or '..' in pathlib.PurePosixPath(row.filename).parts for row in archive.infolist()):
        raise SystemExit('MiVPN product ZIP contract rejected.')
if verify:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise SystemExit('Connection verification requires an explicit HTTPS JSON {ip} endpoint.')
    if parsed.hostname == 'localhost' or parsed.hostname.endswith('.localhost'):
        raise SystemExit('Loopback egress endpoint rejected.')
    try:
        address = ipaddress.ip_address(parsed.hostname.strip('[]'))
        if not address.is_global: raise SystemExit('Private egress endpoint rejected.')
    except ValueError:
        pass
PY

mkdir -p "$probe/extensions/objojdkhlcekcnhaeoebojmffolmafll"
printf '%s\n' 'Disposable MiVPN native launch probe.' > "$probe/.nomad-mivpn-probe"
python3 -m zipfile -e "$plugin" "$probe/extensions/objojdkhlcekcnhaeoebojmffolmafll"
output="$probe/checks.json"

export NOMAD_MIVPN_TEST=1
export NOMAD_PLUGIN_STORAGE_TEST=mivpn
export NOMAD_PROFILE_DIR="$probe"
export NOMAD_EXTENSION_ROOT="$probe/extensions"
export NOMAD_PLUGIN_ROOT="$probe/plugins"
export ARUPA_DESKTOP_DIR="$arupa"
export NOMAD_DISABLE_UPDATE_CHECK=1
if ((verify_connections)); then
  export NOMAD_MIVPN_CONNECT_TEST=1
  export NOMAD_MIVPN_EGRESS_URL="$egress"
fi

"${launch[@]}" --nativeextensiontest "$output" >"$probe/stdout.log" 2>"$probe/stderr.log" &
pid=$!
deadline=$((SECONDS + timeout_seconds))
while kill -0 "$pid" 2>/dev/null; do
  if ((SECONDS >= deadline)); then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    echo 'MiVPN native launch probe timed out.' >&2
    exit 1
  fi
  sleep 1
done
set +e
wait "$pid"
process_code=$?
set -e

python3 - "$output" "$verify_connections" "$process_code" <<'PY'
import json, pathlib, sys
output, verify, process_code = pathlib.Path(sys.argv[1]), sys.argv[2] == '1', int(sys.argv[3])
if not output.is_file(): raise SystemExit(f'Probe did not produce checks.json; process exit={process_code}')
checks = json.loads(output.read_text())
for row in checks: print(f"{'PASS' if row.get('pass') else 'FAIL'} {row.get('name')}: {row.get('detail')}")
if len(checks) < 5 or any(row.get('pass') is not True for row in checks): raise SystemExit('MiVPN API checks failed.')
report_path = pathlib.Path(str(output) + '.mivpn.json')
report = json.loads(report_path.read_text()) if report_path.is_file() else {}
if report.get('sdkAvailable') is not True: raise SystemExit('MiVPN SDK availability was not confirmed.')
if verify:
    rows = report.get('connectionResults', [])
    required = {('reality_tcp','connect'),('reality_tcp','egress'),('hysteria2','switch'),('hysteria2','egress'),('none','disconnect')}
    actual = {(row.get('protocol'), row.get('stage')) for row in rows if row.get('passed') is True}
    if report.get('connectionsVerified') is not True or not required.issubset(actual):
        raise SystemExit('MiVPN connection/egress/disconnect evidence is incomplete.')
print(f'MiVPN native probe passed: {output.parent}')
PY
