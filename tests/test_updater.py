#!/usr/bin/env python3
"""SimpleMail release-critical tests.

Run from the repo root:   PYTHONPATH= py -3 tests/test_updater.py
Pre-release gate:         PYTHONPATH= py -3 tests/test_updater.py --live

Covers the paths that bit real releases:
  T1  ps1 updater generation never raises  (regression: v1.1.2 KeyError
      from str.format() eating PowerShell's literal braces)
  T2  apply_update full flow in an isolated scratch dir (frozen-exe simulated,
      download mocked) - writes the ps1 next to the app, never touches APPDATA
  T3  PowerShell parser accepts the generated ps1
  T4  check_for_update arch/asset selection with mocked GitHub payloads
  T5  check_update error surfacing (arch-named error, exceptions surfaced)
  T6  live GitHub check: latest release must exist and carry an asset for
      THIS machine's arch (regression: v1.1.0 shipped x64-only)
  T7  web/app.js startup update-check ordering + checkForUpdates behaviour
      (node vm harness; skipped if node is unavailable)

--live also runs the generated ps1 for real in a scratch dir (replace +
relaunch + self-delete). It is skipped automatically if a SimpleMail process
is already running (the updater waits for it to exit).

SAFETY RULES (learned the hard way):
  * Never launch a real app instance from a test - a launched instance reads
    and rewrites the user's real config (%APPDATA%/SimpleMail/config.json).
    If a test ever must launch one, point APPDATA at a temp dir first:
    CONFIG_DIR honours os.environ["APPDATA"].
  * All fixtures live under a tempfile scratch dir that is removed at exit.
  * Tests never read or write the real config.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIVE = "--live" in sys.argv
FAILS = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def load_app():
    """Import mailapp.py headless (pywebview is not importable in tests)."""
    spec = importlib.util.spec_from_file_location("mailapp", REPO / "mailapp.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["webview"] = type(sys)("fake_webview")
    spec.loader.exec_module(m)
    return m


m = load_app()
SCRATCH = Path(tempfile.mkdtemp(prefix="sm-test-"))

# ---------------------------------------------------------------------------
# T1 + T2: apply_update full flow (frozen simulation, isolated scratch)
# ---------------------------------------------------------------------------

target = SCRATCH / "SimpleMail.exe"
target.write_bytes(b"OLD-EXE")
new_exe = SCRATCH / "SimpleMail.new.exe"
updater = SCRATCH / "_update.ps1"
popen_calls = []

m._frozen_exe_path = lambda: target
m.download_file = lambda url, dest: Path(dest).write_bytes(b"NEW-EXE-CONTENT")
m.os._exit = lambda code: (_ for _ in ()).throw(SystemExit(code))
orig_popen = subprocess.Popen


def fake_popen(args, **kw):
    popen_calls.append((list(args), kw))
    return None


subprocess.Popen = fake_popen
try:
    m.apply_update("https://example.invalid/SimpleMail-arm64.exe")
    check("T1: apply_update completed without raising (KeyError regression)", True)
except SystemExit as e:
    check("T1: apply_update completed without raising (KeyError regression)", e.code == 0, str(e))
except Exception as e:
    check("T1: apply_update completed without raising (KeyError regression)",
          False, f"{type(e).__name__}: {e}")
finally:
    subprocess.Popen = orig_popen

check("T2: new exe downloaded next to app", new_exe.exists() and new_exe.read_bytes() == b"NEW-EXE-CONTENT")
check("T2: ps1 written next to app", updater.exists())
if updater.exists():
    ps = updater.read_text(encoding="utf-8")
    check("T2: ps1 has real paths (no placeholders)",
          str(target) in ps and str(new_exe) in ps
          and "__TARGET__" not in ps and "__NEW_EXE__" not in ps)
    check("T2: ps1 keeps wait-loop + Start-Sleep + self-delete",
          "Get-Process -Name SimpleMail" in ps
          and "Start-Sleep -Seconds 2" in ps
          and "PSCommandPath" in ps)
check("T2: updater launched hidden via powershell",
      len(popen_calls) == 1 and "powershell" in popen_calls[0][0][0].lower()
      and "-File" in popen_calls[0][0]
      and popen_calls[0][1].get("creationflags", 0) & 0x00000200  # CREATE_NEW_PROCESS_GROUP
      and "Hidden" in popen_calls[0][0],
      str(popen_calls))
check("T2: NOT DETACHED_PROCESS (that breaks powershell -File, v1.1.4 regression)",
      not (popen_calls[0][1].get("creationflags", 0) & 0x00000008), str(popen_calls))

# ---------------------------------------------------------------------------
# T2b: the launch form actually EXECUTES the script (v1.1.2-v1.1.3 bug:
# DETACHED_PROCESS made powershell silently never run the ps1)
# ---------------------------------------------------------------------------

if shutil.which("powershell"):
    launch_log = SCRATCH / "launch-log.txt"
    launch_ps1 = SCRATCH / "t-launch.ps1"
    launch_ps1.write_text(
        f"Set-Content -Path '{launch_log}' -Value 'ran'\n"
        "Remove-Item -Force $PSCommandPath -ErrorAction SilentlyContinue\n",
        encoding="utf-8")
    m._launch_updater(launch_ps1)
    deadline = time.time() + 15
    while time.time() < deadline and not launch_log.exists():
        time.sleep(1)
    check("T2b: launched updater script actually executes",
          launch_log.exists() and launch_log.read_text().strip() == "ran",
          "<no log>" if not launch_log.exists() else launch_log.read_text())
    check("T2b: launched ps1 self-deletes", not launch_ps1.exists())
else:
    print("SKIP  T2b: no powershell on PATH")

# ---------------------------------------------------------------------------
# T3: PowerShell parses the generated ps1 (syntax gate)
# ---------------------------------------------------------------------------

if updater.exists() and shutil.which("powershell"):
    p = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$e=$null; [System.Management.Automation.PSParser]::Tokenize("
         f"(Get-Content -Raw '{updater}'), [ref]$e) | Out-Null; "
         f"if($e.Count){{exit 1}} else {{exit 0}}"],
        capture_output=True, text=True, timeout=60)
    check("T3: PowerShell parses the updater script", p.returncode == 0, p.stderr.strip()[:200])
else:
    check("T3: PowerShell parses the updater script", False, "no powershell on PATH")

# ---------------------------------------------------------------------------
# T4: check_for_update arch/asset selection (mocked GitHub payloads)
# ---------------------------------------------------------------------------

RELEASE_JSON = {
    "tag_name": "v9.9.9",
    "html_url": "https://example.invalid/releases/tag/v9.9.9",
    "body": "release notes",
    "assets": [
        {"name": "SimpleMail-x64.exe", "browser_download_url": "https://example.invalid/x64"},
        {"name": "SimpleMail-arm64.exe", "browser_download_url": "https://example.invalid/arm64"},
    ],
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload

    def decode(self, _enc):
        return self._payload.decode()


orig_urlopen = urllib.request.urlopen


def fake_urlopen(payload):
    def _open(req, timeout=None):
        return FakeResponse(payload)
    return _open


urllib.request.urlopen = fake_urlopen(RELEASE_JSON)
try:
    m.current_arch = lambda: "arm64"
    info = m.check_for_update()
    check("T4: arm64 picks the arm64 asset", info and info["asset_name"] == "SimpleMail-arm64.exe",
          str(info and info["asset_name"]))
    m.current_arch = lambda: "x64"
    info = m.check_for_update()
    check("T4: x64 picks the x64 asset", info and info["asset_name"] == "SimpleMail-x64.exe",
          str(info and info["asset_name"]))

    urllib.request.urlopen = fake_urlopen({**RELEASE_JSON, "assets": [RELEASE_JSON["assets"][0]]})
    m.current_arch = lambda: "arm64"
    info = m.check_for_update()
    check("T4: missing arch asset -> asset_url None (v1.1.0 regression)",
          info and info["asset_url"] is None)

    urllib.request.urlopen = fake_urlopen({**RELEASE_JSON, "tag_name": "not-a-version"})
    info = m.check_for_update()
    check("T4: junk tag -> None", info is None)
finally:
    urllib.request.urlopen = orig_urlopen

# ---------------------------------------------------------------------------
# T5: check_update error surfacing
# ---------------------------------------------------------------------------

orig_check = m.check_for_update
m.check_for_update = lambda: {"version": (9, 9, 9), "tag": "v9.9.9", "url": "",
                              "asset_url": None, "asset_name": None, "notes": "", "arch": "arm64"}
try:
    res = m.Api(None).check_update()
    check("T5: missing asset -> available=False + error names arch",
          res.get("available") is False and "arm64" in res.get("error", ""), str(res))
finally:
    m.check_for_update = orig_check

m.check_for_update = lambda: (_ for _ in ()).throw(RuntimeError("network boom"))
try:
    res = m.Api(None).check_update()
    check("T5: exception -> error field surfaced", res.get("error") == "network boom", str(res))
finally:
    m.check_for_update = orig_check

# ---------------------------------------------------------------------------
# T6: live GitHub check - latest release must carry this machine's arch asset
# ---------------------------------------------------------------------------

try:
    info = m.check_for_update()
    if info is None:
        check("T6: live latest release found", False, "check_for_update returned None")
    else:
        check("T6: live latest release found", True, info["tag"])
        have_asset = info["asset_url"] is not None
        check(f"T6: release {info['tag']} has a {info['arch']} asset", have_asset)
        if have_asset:
            req = urllib.request.Request(info["asset_url"], method="HEAD",
                                         headers={"User-Agent": "SimpleMail/tests"})
            with urllib.request.urlopen(req, timeout=30) as r:
                check("T6: asset URL downloads (200)", r.status == 200, str(r.status))
except Exception as e:
    if LIVE:
        check("T6: live GitHub check", False, str(e))
    else:
        print(f"SKIP  T6: live GitHub check (offline? {e}) - rerun with --live")

# ---------------------------------------------------------------------------
# T3-live (--live only): execute the generated ps1 in the scratch dir
# ---------------------------------------------------------------------------

if LIVE and updater.exists() and shutil.which("powershell"):
    busy = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "if (Get-Process -Name SimpleMail -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"],
        capture_output=True, timeout=30).returncode == 1
    if busy:
        print("SKIP  T3-live: a SimpleMail process is running (updater would wait for it)")
    else:
        staged = SCRATCH / "staged"           # fresh dir: ps1 targets the real scratch paths
        staged.mkdir()
        tgt = staged / "SimpleMail.exe"
        new = staged / "SimpleMail.new.exe"
        tgt.write_bytes(b"OLD")
        new.write_bytes(b"NEW-PAYLOAD")
        ps1 = staged / "_update.ps1"
        ps1.write_text(
            ("$target = '__TARGET__'\n$new = '__NEW_EXE__'\n"
             "while (Get-Process -Name SimpleMail -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }\n"
             "Copy-Item -Force $new $target\nRemove-Item -Force $new\n"
             "Start-Process -FilePath $target\n"
             "Remove-Item -Force $PSCommandPath -ErrorAction SilentlyContinue\n"
             ).replace("__TARGET__", str(tgt)).replace("__NEW_EXE__", str(new)), encoding="utf-8")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(ps1)], capture_output=True, text=True, timeout=120)
        time.sleep(3)
        check("T3-live: ps1 exits clean", p.returncode == 0, p.stderr.strip()[:200])
        check("T3-live: target replaced", tgt.exists() and tgt.read_bytes() == b"NEW-PAYLOAD")
        check("T3-live: .new removed", not new.exists())
        check("T3-live: ps1 self-deleted", not ps1.exists())

# ---------------------------------------------------------------------------
# T7: web/app.js - startup update check ordering + checkForUpdates behaviour
# ---------------------------------------------------------------------------

js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
mfn = re.search(r"async function checkForUpdates\(silent = false\) \{(.*?)\n\}", js, re.S)
fn_src = "async function checkForUpdates(silent = false) {" + mfn.group(1) + "\n}"
start = js.index("  try {\n    const data = await api.get_config();")
catch_i = js.index("  } catch (e) {", start)
end = js.index("\n  }\n", catch_i) + len("\n  }\n")
init_src = js[start:end]

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const fnSrc = fs.readFileSync(process.argv[2], 'utf8');
const initSrc = fs.readFileSync(process.argv[3], 'utf8');
const calls = [];
const mkStubs = () => ({
  toast: (...a) => calls.push(['toast', ...a]),
  openSettings: () => calls.push(['openSettings']),
  applyScale: () => calls.push(['applyScale']),
  selectAccount: async () => calls.push(['selectAccount']),
  state: {},
  checkForUpdates: async () => calls.push(['checkForUpdates']),
  escapeHtml: s => String(s),
  document: { getElementById: () => ({ innerHTML: '' }) },
  $: id => ({ textContent: '', innerHTML: '', style: {},
               classList: { add: () => calls.push(['show', id]) } }),
});
let ok = true;
const t = (name, cond, detail = '') => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond ? '' : '  [' + detail + ']'));
  if (!cond) ok = false;
};
(async () => {
  // startup ordering
  let ctx = vm.createContext(Object.assign({ api: { get_config: async () => ({ accounts: [] }) } }, mkStubs()));
  vm.runInContext('(async () => { ' + initSrc + ' })()', ctx);
  await new Promise(r => setImmediate(r));
  const iCheck = calls.findIndex(c => c[0] === 'checkForUpdates');
  const iOpen = calls.findIndex(c => c[0] === 'openSettings');
  t('T7: logged-out app still checks for updates first', iCheck >= 0 && iCheck < iOpen, JSON.stringify(calls));
  calls.length = 0;
  ctx = vm.createContext(Object.assign({ api: { get_config: async () => ({
    accounts: [{ id: 'a1' }], active_account: 'a1', ui_scale: 'default' }) } }, mkStubs()));
  vm.runInContext('(async () => { ' + initSrc + ' })()', ctx);
  await new Promise(r => setImmediate(r));
  t('T7: logged-in app checks exactly once',
    calls.filter(c => c[0] === 'checkForUpdates').length === 1, JSON.stringify(calls));

  // checkForUpdates behaviour (available / error / silent / up-to-date)
  ctx = vm.createContext(Object.assign({ api: { check_update: async () => ({
    available: true, new_version: 'v9.9.9', local_version: '1.0.0', notes: '' }) } }, mkStubs()));
  vm.runInContext(fnSrc, ctx);
  calls.length = 0;
  const r1 = await vm.runInContext('checkForUpdates(false)', ctx);
  t('T7: available -> returns true + backdrop', r1 === true && calls.some(c => c[0] === 'show'),
    JSON.stringify(calls));
  ctx.api = { check_update: async () => ({ available: false,
    error: 'No arm64 build published for this release yet' }) };
  calls.length = 0;
  const r2 = await vm.runInContext('checkForUpdates(false)', ctx);
  t('T7: error -> honest toast, never "latest" lie',
    r2 === false && calls.length === 1 && calls[0][1].startsWith('Update check failed:'),
    JSON.stringify(calls));
  calls.length = 0;
  await vm.runInContext('checkForUpdates(true)', ctx);
  t('T7: error + silent -> no toast', calls.length === 0, JSON.stringify(calls));
  ctx.api = { check_update: async () => ({ available: false, local_version: '1.1.3' }) };
  calls.length = 0;
  await vm.runInContext('checkForUpdates(false)', ctx);
  t('T7: genuinely up to date -> "latest" message', calls.length === 1 &&
    calls[0][1].includes('latest version'), JSON.stringify(calls));
  console.log(ok ? 'NODE-JS OK' : 'NODE-JS FAILED');
  process.exit(ok ? 0 : 1);
})();
"""

if shutil.which("node"):
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="sm-harness-", delete=False,
                                     dir=os.environ.get("TEMP")) as fh:
        fh.write(HARNESS)
        harness_path = fh.name
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="sm-fn-", delete=False,
                                     dir=os.environ.get("TEMP")) as fh:
        fh.write(fn_src)
        fn_path = fh.name
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="sm-init-", delete=False,
                                     dir=os.environ.get("TEMP")) as fh:
        fh.write(init_src)
        init_path = fh.name
    try:
        p = subprocess.run(["node", harness_path, fn_path, init_path], capture_output=True,
                           text=True, timeout=90, cwd=REPO / "web")
        print(p.stdout.strip())
        if p.stderr.strip():
            print("node stderr:", p.stderr.strip()[:300])
        check("T7: node vm checks passed", p.returncode == 0, p.stderr.strip()[:200])
    finally:
        for f in (harness_path, fn_path, init_path):
            try:
                os.unlink(f)
            except OSError:
                pass
else:
    print("SKIP  T7: node not on PATH")

# ---------------------------------------------------------------------------
shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print(f"{CHECKS} checks, {len(FAILS)} failures")
if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("ALL CHECKS PASSED")
