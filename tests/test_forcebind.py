"""Tests for lanparty\\forcebind.py: in an internet party, native games start through ForceBindIP bound to the VPN IP.
Run: python tests\\test_forcebind.py"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import forcebind, launcher  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg:
    def __init__(self, d, **kw):
        self.data_dir = d
        self.data = dict(kw)

    def get(self, k, default=None):
        return self.data.get(k, default)

    def __getitem__(self, k):
        return self.data[k]

    def set(self, k, v):
        self.data[k] = v


def fake_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


sha = lambda b: hashlib.sha256(b).hexdigest().upper()  # noqa: E731
tmp = tempfile.mkdtemp(prefix="lp_fbi_")
real = (forcebind.FILES, forcebind.ZIP_SHA256, forcebind.vc_runtime_ok, forcebind.exe_arch)
try:
    blobs = {n: f"fake {n}".encode() for n in real[0]}
    good = fake_zip(dict(blobs, **{"readme.txt": b"x", "..\\evil.exe": b"evil"}))
    forcebind.FILES = {n: sha(b) for n, b in blobs.items()}
    forcebind.ZIP_SHA256 = sha(good)
    forcebind.vc_runtime_ok = lambda arch: True
    data = os.path.join(tmp, "data")

    # ---- fetching ----
    def boom(url):
        raise OSError("no internet")
    check("couldn't download" in forcebind.ensure(data, boom), "download failure reported")
    check("doesn't match" in forcebind.ensure(data, lambda u: good + b"x"), "changed zip refused")
    bad_inner = fake_zip(dict(blobs, **{"BindIP.dll": b"tampered"}))
    forcebind.ZIP_SHA256 = sha(bad_inner)
    check("doesn't match its pinned" in forcebind.ensure(data, lambda u: bad_inner), "changed file inside zip refused")
    check(not os.path.isdir(forcebind.tools_dir(data)) or not os.listdir(forcebind.tools_dir(data)),
          "nothing written from a bad zip")
    forcebind.ZIP_SHA256 = sha(good)
    check(forcebind.ensure(data, lambda u: good) == "ok", "pinned zip accepted")
    check(sorted(os.listdir(forcebind.tools_dir(data))) == sorted(blobs), "only the four known files are written")
    check(not os.path.exists(os.path.join(data, "tools", "evil.exe")) and not os.path.exists(os.path.join(data, "evil.exe")),
          "no path from the zip is used")
    check(forcebind.ensure(data, boom) == "ok", "already there: no download")

    # ---- argv ----
    gdir = os.path.join(tmp, "Game")
    os.makedirs(gdir)
    exe = os.path.join(gdir, "game.exe")
    open(exe, "wb").close()
    game = {"id": "g1", "title": "Game", "path": gdir, "exe": "game.exe", "kind": "pe", "arch": "x86", "lan": {}}
    cfg = Cfg(data)
    td = forcebind.tools_dir(data)
    forcebind.exe_arch = lambda p: "x86"
    info = []
    a = forcebind.maybe_wrap(game, [exe, "-connect", "10.128.0.1"], "10.128.0.10", cfg, data, info)
    check(a == [os.path.join(td, "ForceBindIP.exe"), "10.128.0.10", exe, "-connect", "10.128.0.1"],
          f"32-bit game: ForceBindIP.exe <vpn ip> game args ({a})")
    check(any("force-bound to 10.128.0.10" in m for m in info), "launch notes say it was force-bound")
    forcebind.exe_arch = lambda p: "x64"
    a = forcebind.maybe_wrap(game, [exe], "10.128.0.10", cfg, data, [])
    check(a[0].endswith("ForceBindIP64.exe") and a[1:] == ["10.128.0.10", exe], "64-bit game: ForceBindIP64.exe")
    forcebind.exe_arch = lambda p: ""
    a = forcebind.maybe_wrap(dict(game, arch="x64"), [exe], "10.128.0.10", cfg, data, [])
    check(a[0].endswith("ForceBindIP64.exe"), "unknown exe arch: the game's arch is used")
    forcebind.exe_arch = lambda p: "x86"
    a = forcebind.maybe_wrap(dict(game, lan={"force_bind": "delayed"}), [exe], "10.128.0.10", cfg, data, [])
    check(a[1:3] == ["-i", "10.128.0.10"], "lan.json force_bind 'delayed' adds -i")

    # ---- when it doesn't apply ----
    check(forcebind.maybe_wrap(game, [exe], "", cfg, data, []) == [exe], "not in a party: started normally")
    i2 = []
    check(forcebind.maybe_wrap(dict(game, lan={"force_bind": False}), [exe], "10.128.0.10", cfg, data, i2) == [exe]
          and any("off" in m for m in i2), "lan.json force_bind false: off, and the notes say so")
    check(forcebind.maybe_wrap(game, [exe], "10.128.0.10", Cfg(data, no_force_bind=["g1"]), data, []) == [exe],
          "per-PC no_force_bind: off")
    check(forcebind.maybe_wrap(game, ["cmd.exe", "/c", "start.bat"], "10.128.0.10", cfg, data, []) ==
          ["cmd.exe", "/c", "start.bat"], ".bat start: not wrapped")
    check(forcebind.maybe_wrap(dict(game, kind="dos"), ["dosbox.exe", "-conf", "x"], "10.128.0.10", cfg, data, [])
          == ["dosbox.exe", "-conf", "x"], "DOS game: not wrapped")
    check(forcebind.maybe_wrap(dict(game, network="ipx"), [exe], "10.128.0.10", cfg, data, []) == [exe],
          "IPX game: not wrapped (IPXWrapper)")
    forcebind.vc_runtime_ok = lambda arch: False
    i3 = []
    check(forcebind.maybe_wrap(game, [exe], "10.128.0.10", cfg, data, i3) == [exe] and any("Visual C++" in m for m in i3),
          "no VC++ runtime: started normally with a note")
    forcebind.vc_runtime_ok = lambda arch: True
    shutil.rmtree(td)
    real_ensure = forcebind.ensure
    forcebind.ensure = lambda d, fetch=None: real_ensure(d, boom)
    i4 = []
    check(forcebind.maybe_wrap(game, [exe], "10.128.0.10", cfg, data, i4) == [exe] and
          any("Started normally" in m for m in i4), "fetch fails at launch: game still starts, note says why")
    forcebind.ensure = real_ensure
    forcebind.ensure(data, lambda u: good)

    # ---- through launcher.launch (Play / Host / then_join) ----
    started = []
    real_start = launcher.start
    launcher.start = lambda argv, cwd, as_admin=False, info=None: started.append((argv, as_admin)) or "user"
    try:
        r = launcher.launch(game, None, "solo", "", "Player", cfg, [], [], "10.128.0.10")
        check(r == "ok" and started[-1][0][0].endswith("ForceBindIP.exe") and started[-1][0][2] == exe,
              "launch in a party goes through ForceBindIP")
        launcher.launch(game, None, "solo", "", "Player", cfg, [], [], "")
        check(started[-1][0] == [exe], "launch outside a party: the game itself")
        launcher.launch(game, None, "solo", "", "Player", Cfg(data, run_as_admin=["g1"]), [], [], "10.128.0.10")
        check(started[-1][1] is True and started[-1][0][0].endswith("ForceBindIP.exe"), "run_as_admin games too")
    finally:
        launcher.start = real_start
    check(launcher.start.__name__ == "start", "start restored")

    # the real pinned file list is complete
    check(set(real[0]) == {"ForceBindIP.exe", "BindIP.dll", "ForceBindIP64.exe", "BindIP64.dll"} and
          all(len(h) == 64 for h in real[0].values()) and len(real[1]) == 64, "real pins: 4 files + zip SHA-256")
finally:
    forcebind.FILES, forcebind.ZIP_SHA256, forcebind.vc_runtime_ok, forcebind.exe_arch = real
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
