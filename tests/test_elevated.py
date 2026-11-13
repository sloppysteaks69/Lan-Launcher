"""Tests for prereqs.run_elevated when the launcher already runs as admin (run_admin.bat): no prompt, runs
directly, output copied to the log. And netwrap.open_firewall reporting whether the rules were added.
Run: python tests\\test_elevated.py"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import netwrap, prereqs  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg:
    def __init__(self, d):
        self.data_dir = d

    def sub(self, *parts):
        p = os.path.join(self.data_dir, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def get(self, k, default=None):
        return default

    def __getitem__(self, k):
        return {"folders": []}[k]


tmp = tempfile.mkdtemp(prefix="lp_elev_")
real_admin = prereqs.is_admin
try:
    cfg = Cfg(tmp)
    prereqs.is_admin = lambda: True            # as when started with run_admin.bat
    log = []
    rc = prereqs.run_elevated(cfg, ["Write-Output 'step one'", "Write-Output 'Firewall rules added.'"], log.append)
    check(rc == 0, f"admin: script ran directly (rc {rc})")
    check(any("no prompt needed" in m for m in log), "admin: says no prompt is needed")
    check("step one" in log and "Firewall rules added." in log and "ELEVATED_DONE" in log,
          f"admin: script output copied to the log ({log})")
    check(not any("admin prompt" in m.lower() and "waiting" in m.lower() for m in log), "admin: no 'waiting for prompt'")
    log = []
    rc = prereqs.run_elevated(cfg, ["throw 'boom'"], log.append)
    check(any("boom" in m for m in log), "admin: errors reach the log")

    # open_firewall says whether the rules were really added
    net = netwrap.NetWrap.__new__(netwrap.NetWrap)
    net.cfg = cfg
    net.firewall_lines = lambda games: ["Write-Output 'Firewall rules added.'"]
    real_run = netwrap.run_elevated
    try:
        netwrap.run_elevated = lambda c, lines, lg: (lg("Firewall rules added."), 0)[1]
        check(net.open_firewall([], lambda m: None) is True, "firewall: success reported")
        netwrap.run_elevated = lambda c, lines, lg: (lg("You said No to the admin prompt: nothing was changed."), 1)[1]
        out = []
        check(net.open_firewall([], out.append) is False and "said No" in out[0], "firewall: declined prompt reported")
    finally:
        netwrap.run_elevated = real_run
finally:
    prereqs.is_admin = real_admin
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
