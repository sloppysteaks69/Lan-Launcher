"""Duke Nukem Forever: 16-player host and direct-IP join, from lan.json and from data\\known_games.json.
Run: python tests\\test_dnf.py"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import launcher  # noqa: E402
from lanparty.library import Library  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg:
    def __init__(self, d, folders):
        self.data_dir = d
        os.makedirs(d, exist_ok=True)
        self.data = {"folders": folders}

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, k, v):
        self.data[k] = v

    def sub(self, *parts):
        p = os.path.join(self.data_dir, *parts)
        os.makedirs(p, exist_ok=True)
        return p


EXAMPLE = os.path.join(ROOT, "docs", "examples", "Duke Nukem Forever", "lan.json")
with open(EXAMPLE, encoding="utf-8") as f:
    ex = json.load(f)
host_arg = ex["host"]["args"][0]
check("?Listen" in host_arg and "?MaxPlayers=16" in host_arg, "example host command is a 16-player listen server")
check(ex["players"]["max"] == 16, "example lists 16 players")
check(ex["join"]["args"] == ["{host_ip}:7777"], "example joins by IP on 7777")

tmp = tempfile.mkdtemp(prefix="lp_dnf_")
try:
    games = os.path.join(tmp, "Games")
    with_lan = os.path.join(games, "Duke Nukem Forever Complete")
    bare = os.path.join(tmp, "Games2", "Duke Nukem Forever")
    for g in (with_lan, bare):
        os.makedirs(os.path.join(g, "System"))
        with open(os.path.join(g, "System", "DukeForever.exe"), "wb") as f:
            f.write(b"MZ" + b"\0" * 62)
        with open(os.path.join(g, "System", "Launcher.exe"), "wb") as f:
            f.write(b"MZ" + b"\0" * 62)
    shutil.copy(EXAMPLE, os.path.join(with_lan, "lan.json"))

    lib = Library(Cfg(os.path.join(tmp, "data"), [games, os.path.dirname(bare)]))
    lib.rescan()
    check(sorted(lib.games) == ["dukenukemforevercomplete"], "both folders get the same id as before: "
          + ",".join(sorted(lib.games)))
    recs = [lib.scan_game(with_lan), lib.scan_game(bare)]
    for rec, label in zip(recs, ("with lan.json", "no lan.json (known_games)")):
        check(rec["id"] == "dukenukemforevercomplete", f"{label}: id unchanged")
        check(rec["exe"].lower() == "system\\dukeforever.exe", f"{label}: exe is System\\DukeForever.exe ({rec['exe']})")
        check(rec["players"].get("max") == 16, f"{label}: 16 players")
        argv, cwd = launcher.build_argv(rec, "host", "", "Player", "10.128.0.7")
        check(argv[-1] == "DM-Industrial?Game=dnGame.dnDeathmatchGame?Listen?MaxPlayers=16?Rotation=Random",
              f"{label}: host argv {argv[1:]}")
        check(os.path.basename(cwd).lower() == "system", f"{label}: starts in System")
        argv, _ = launcher.build_argv(rec, "join", "10.128.0.3", "Player", "10.128.0.7")
        check(argv[1:] == ["10.128.0.3:7777"], f"{label}: join argv {argv[1:]}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
