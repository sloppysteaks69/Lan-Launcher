"""The bundled DOSBox-X runs a DOS game from the config the launcher writes.
A fake DOS game (GAME.BAT in a sub folder) writes RESULT.TXT; DOSBox-X runs with -silent so no window opens.
Run: python tests\\test_dosbox.py"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import netwrap  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg(dict):
    def __init__(self, base):
        super().__init__(dosbox_path="")
        self.base = base

    def sub(self, *parts):
        p = os.path.join(self.base, *parts)
        os.makedirs(p, exist_ok=True)
        return p


tmp = tempfile.mkdtemp(prefix="lp_dosbox_")
game_dir = os.path.join(tmp, "Old Game")
os.makedirs(os.path.join(game_dir, "BIN"))
with open(os.path.join(game_dir, "BIN", "GAME.BAT"), "w", newline="\r\n") as fh:
    fh.write("@echo off\necho started %1 > C:\\RESULT.TXT\n")

net = netwrap.NetWrap(Cfg(os.path.join(tmp, "data")))
db = net.dosbox_path()
check(db == netwrap.BUNDLED_DOSBOX, f"bundled DOSBox-X is the default ({db})")
game = {"id": "oldgame", "path": game_dir, "exe": "BIN/GAME.BAT"}

conf = net.dosbox_conf(game, "solo", "", "SOLO")
argv = net.dosbox_argv(db, conf)
check("-fastlaunch" in argv and "-nopromptfolder" in argv, f"DOSBox-X flags: {argv[1:]}")
r = subprocess.run(argv + ["-silent"], cwd=game_dir, capture_output=True, timeout=60)
res = os.path.join(game_dir, "RESULT.TXT")
txt = open(res).read().strip() if os.path.exists(res) else ""
check(txt.lower() == "started solo", f"game ran in DOSBox-X with its args (exit {r.returncode}, RESULT.TXT={txt!r})")

host_conf = open(net.dosbox_conf(game, "host", "", "")).read()
join_conf = open(net.dosbox_conf(game, "join", "10.128.0.1", "")).read()
check("ipx=true" in host_conf and "ipxnet startserver 21300" in host_conf, "host config starts the IPX server")
check("ipxnet connect 10.128.0.1 21300" in join_conf, "join config connects to the host over IPX")

net.cfg["dosbox_path"] = sys.executable          # any existing file stands in for a hand-picked DOSBox
check(net.dosbox_path() == sys.executable, "a DOSBox path set in Settings still wins")
check(net.dosbox_argv(sys.executable, "x.conf") == [sys.executable, "-conf", "x.conf"],
      "plain DOSBox gets no DOSBox-X flags")

shutil.rmtree(tmp, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("\nALL_PASS")
