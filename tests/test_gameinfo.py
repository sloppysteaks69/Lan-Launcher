"""Tests for the library filter data: genre / year / players from data\\game_info.json and lan.json.
Run: python tests\\test_gameinfo.py"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty.known import GameInfo, INFO_PATH, norm  # noqa: E402
from lanparty.library import Library  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg:
    def __init__(self, d, folders):
        self.data_dir = d
        os.makedirs(d, exist_ok=True)      # config.Config makes it too
        self.data = {"folders": folders}

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)

    def sub(self, *parts):
        p = os.path.join(self.data_dir, *parts)
        os.makedirs(p, exist_ok=True)
        return p


# ---- the data file itself ----
with open(INFO_PATH, encoding="utf-8") as f:
    db = json.load(f)["games"]
keys = [k for g in db for k in g["keys"]]
check(len(keys) == len(set(keys)), "no key listed twice")
check(all(k == norm(k) for k in keys), "keys are normalised")
check(all(1970 <= g["year"] <= 2100 for g in db if "year" in g), "years are plausible")
check(all(1 <= g["players"]["min"] <= g["players"]["max"] for g in db if "players" in g), "player ranges are valid")
check(all(isinstance(g.get("genre", ""), str) for g in db), "genres are text")

gi = GameInfo()
check(gi.lookup("Age of Empires 3") == {"genre": "RTS", "year": 2005, "players": {"min": 2, "max": 8}},
      "lookup by title")
check(gi.lookup("nothing", "cnc generals zero hour").get("year") == 2003, "lookup falls back to the folder name")
check(gi.lookup("Beyond-All-Reason") == {"genre": "RTS"}, "unknown year left out, not guessed")
check(gi.lookup("Some Homebrew Game") == {}, "unknown game: nothing")

# ---- library records: lan.json overrides game_info, ids unchanged ----
tmp = tempfile.mkdtemp(prefix="lp_gi_")
try:
    games = os.path.join(tmp, "Games")
    for name in ("Homeworld", "Hexen 2", "My Mod"):
        os.makedirs(os.path.join(games, name))
        open(os.path.join(games, name, "game.exe"), "wb").close()
    with open(os.path.join(games, "Hexen 2", "lan.json"), "w", encoding="utf-8") as f:
        json.dump({"game": "Hexen 2", "players": {"min": 2, "max": 16}, "genre": "Shooter"}, f)
    with open(os.path.join(games, "My Mod", "lan.json"), "w", encoding="utf-8") as f:
        json.dump({"genre": "Racing", "year": 2024, "players": {"min": 1, "max": 6}}, f)
    lib = Library(Cfg(os.path.join(tmp, "data"), [games]))
    lib.rescan()
    hw, hx, mm = lib.get("homeworld"), lib.get("hexen2"), lib.get("mymod")
    check(hw and hw["genre"] == "RTS" and hw["year"] == 1999 and hw["players"] == {"min": 2, "max": 8},
          "Homeworld gets genre, year and players")
    check(hx and hx["genre"] == "Shooter" and hx["year"] == 1997 and hx["players"]["max"] == 16,
          "lan.json genre/players override, year from game_info")
    check(mm and mm["genre"] == "Racing" and mm["year"] == 2024 and mm["players"] == {"min": 1, "max": 6},
          "an unknown game can set its own via lan.json")
    check(sorted(lib.games) == ["hexen2", "homeworld", "mymod"], "ids unchanged by the info lookup")
    check("genre" in lib.public_list()[0], "peers see the filter data")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
