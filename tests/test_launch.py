"""Unit tests for how games are started: as the normal user when the launcher is elevated, with a fallback and a
per-PC opt-out. No real process is started. Run: python tests\\test_launch.py"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import launcher  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


calls = []


class FakePopen:
    def __init__(self, argv, cwd=None, **kw):
        calls.append(("popen", list(argv), cwd))


def fake_unelevated(argv, cwd):
    calls.append(("user", list(argv), cwd))
    return 1234


def broken_unelevated(argv, cwd):
    raise OSError("CreateProcessWithTokenW failed: 1314")


class Net:
    def wrapper_installed(self, g):
        return True


real = (launcher.is_elevated, launcher._popen_unelevated, launcher.subprocess.Popen, launcher.namewrite.apply)
tmp = tempfile.mkdtemp(prefix="lanparty_launch_")
try:
    gdir = os.path.join(tmp, "Dark Reign 2")
    os.makedirs(gdir)
    exe = os.path.join(gdir, "dr2.exe")
    open(exe, "wb").close()
    game = {"id": "darkreign2", "title": "Dark Reign 2", "path": gdir, "exe": "dr2.exe", "kind": "pe", "lan": {}}
    launcher.subprocess.Popen = FakePopen
    launcher._popen_unelevated = fake_unelevated
    launcher.namewrite.apply = lambda g, n, ip="": []

    # elevated launcher: the game runs as the normal user, same exe, args and folder
    launcher.is_elevated = lambda: True
    calls.clear()
    info = []
    r = launcher.launch(game, Net(), "solo", "", "Player", Cfg(), [], info)
    check(r == "ok" and calls == [("user", [os.path.realpath(exe)], os.path.dirname(os.path.realpath(exe)))],
          "elevated: started with the user's token, same exe and folder")
    check(info == ["Started dr2.exe as your normal user (not administrator)."], "the way it started is logged")

    # the token start fails: plain start, logged
    launcher._popen_unelevated = broken_unelevated
    calls.clear()
    info = []
    launcher.launch(game, Net(), "solo", "", "Player", Cfg(), [], info)
    check([c[0] for c in calls] == ["popen"], "fallback: plain start")
    check(len(info) == 1 and "as administrator" in info[0] and "1314" in info[0], "fallback logged with the reason")
    launcher._popen_unelevated = fake_unelevated

    # per-PC opt-out
    calls.clear()
    info = []
    launcher.launch(game, Net(), "solo", "", "Player", Cfg(run_as_admin=["darkreign2"]), [], info)
    check([c[0] for c in calls] == ["popen"] and "run_as_admin" in info[0], "run_as_admin setting keeps admin")

    # a lan.json can't ask for admin (it may come from someone else's PC)
    calls.clear()
    g2 = dict(game, lan={"run_as_admin": True}, run_as_admin=True)
    launcher.launch(g2, Net(), "solo", "", "Player", Cfg(), [], [])
    check([c[0] for c in calls] == ["user"], "lan.json/game record can't request admin")

    # not elevated: plain start, nothing special
    launcher.is_elevated = lambda: False
    calls.clear()
    info = []
    launcher.launch(game, Net(), "solo", "", "Player", Cfg(), [], info)
    check([c[0] for c in calls] == ["popen"] and info == [], "not elevated: plain start")

    # host/join args kept
    launcher.is_elevated = lambda: True
    calls.clear()
    g3 = dict(game, join_args="-connect {host_ip}")
    launcher.launch(g3, Net(), "join", "10.128.0.1", "Player", Cfg(), [], [])
    check(calls and calls[0][0] == "user" and calls[0][1][1:] == ["-connect", "10.128.0.1"], "join args kept")

    # {my_ip}: the VPN IP when given, never empty otherwise
    hdir = os.path.join(tmp, "Hexen 2")
    os.makedirs(hdir)
    hexe = os.path.join(hdir, "glh2.exe")
    open(hexe, "wb").close()
    hlan = {"host": {"command": "exe", "args": ["-listen", "16", "-ip", "{my_ip}"]},
            "join": {"command": "exe", "args": ["-listen", "16", "-ip", "{my_ip}", "+connect", "{host_ip}"]}}
    hex2 = {"id": "hexen2", "title": "Hexen 2", "path": hdir, "exe": "glh2.exe", "kind": "pe", "arch": "x64",
            "network": "tcp", "lan": hlan}
    argv, _ = launcher.build_argv(hex2, "join", "10.128.0.1", "Player", "10.128.0.7")
    check(argv[1:] == ["-listen", "16", "-ip", "10.128.0.7", "+connect", "10.128.0.1"], "{my_ip}: VPN IP filled in")
    argv, _ = launcher.build_argv(hex2, "host", "", "Player", "")
    check(argv[4] not in ("", "{my_ip}") and argv[4] == launcher.default_ip(), "{my_ip}: falls back to the LAN IP")
    real_sock = launcher.socket.socket

    def no_net(*a, **k):
        raise OSError("no network")
    launcher.socket.socket = no_net
    try:
        check(launcher.resolve_my_ip("") == "127.0.0.1", "{my_ip}: last resort 127.0.0.1")
    finally:
        launcher.socket.socket = real_sock
    calls.clear()
    launcher.launch(hex2, Net(), "host", "10.128.0.1", "Player", Cfg(), [], [], "10.128.0.1")
    check(calls and calls[0][1][1:] == ["-listen", "16", "-ip", "10.128.0.1"], "launch passes my_ip through")

    # then_join self-join goes to my_ip (a game bound with -ip isn't on loopback)
    real_timer = launcher.threading.Timer

    class NowTimer:
        def __init__(self, delay, fn):
            self.fn = fn

        def start(self):
            self.fn()
    launcher.threading.Timer = NowTimer
    try:
        tj = dict(hex2, lan=dict(hlan, host=dict(hlan["host"], then_join=True)))
        calls.clear()
        launcher.launch(tj, Net(), "host", "10.128.0.1", "Player", Cfg(), [], [], "10.128.0.1")
        check(len(calls) == 2 and calls[1][1][-2:] == ["+connect", "10.128.0.1"], "then_join: joins my_ip")
        tj2 = dict(game, lan={"host": {"command": "exe", "args": ["-server"], "then_join": True},
                              "join": {"command": "exe", "args": ["+connect", "{host_ip}"]}})
        calls.clear()
        launcher.launch(tj2, Net(), "host", "", "Player", Cfg(), [], [], "")
        check(len(calls) == 2 and calls[1][1][-1] == "127.0.0.1", "then_join: loopback when not bound to {my_ip}")
    finally:
        launcher.threading.Timer = real_timer

    # IPX-flagged 64-bit game: wrapper skipped with a note, the game still starts
    class NoWrapNet:
        def wrapper_installed(self, g):
            return False

        def install_wrapper(self, g):
            return "IPXWrapper only works with 32-bit games."
    calls.clear()
    info = []
    r = launcher.launch(dict(hex2, network="ipx"), NoWrapNet(), "host", "", "Player", Cfg(), [], info, "10.128.0.1")
    check(r == "ok" and len(calls) == 1, "x64 ipx: starts without the wrapper")
    check(any("IPX wrapper skipped" in i for i in info), "x64 ipx: note added")
    r = launcher.launch(dict(hex2, network="ipx", arch="x86"), NoWrapNet(), "host", "", "Player", Cfg(), [], [])
    check(r.startswith("IPX wrapper:"), "x86 ipx: wrapper still required")

    # game_exe_paths for the firewall
    check(launcher.game_exe_paths(game) == [os.path.realpath(exe)], "game_exe_paths: the exe")
    check(launcher.game_exe_paths(dict(game, exe="..\\..\\evil.exe")) == [], "game_exe_paths: nothing outside")
finally:
    launcher.is_elevated, launcher._popen_unelevated, launcher.subprocess.Popen, launcher.namewrite.apply = real
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL_PASS" if not fails else f"FAILED: {len(fails)}")
sys.exit(1 if fails else 0)
