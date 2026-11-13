"""Tests for the hearts (interest 0-10 per player per game) in lanparty\\vote.py.
Run: python tests\\test_likes.py"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty.vote import Votes, clamp_score  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg:
    def __init__(self, d, pid, name):
        self.data_dir = d
        self.data = {"peer_id": pid, "name": name}

    def __getitem__(self, k):
        return self.data[k]


tmp = tempfile.mkdtemp(prefix="lp_likes_")
try:
    # ---- clamping / half steps ----
    check([clamp_score(x) for x in (-3, 0, 4, 10, 11, 99)] == [0, 0, 4, 10, 10, 10], "scores clamp to 0-10")
    check(clamp_score(6.4) == 6 and clamp_score(6.6) == 7, "half-heart steps: whole numbers")
    check(clamp_score("7") == 7 and clamp_score("x") == 0 and clamp_score(None) == 0, "bad values become 0")

    a_dir, b_dir = os.path.join(tmp, "a"), os.path.join(tmp, "b")
    os.makedirs(a_dir)
    os.makedirs(b_dir)
    a = Votes(Cfg(a_dir, "alice", "Alice"))
    b = Votes(Cfg(b_dir, "bob", "Bob"))
    pushed = []
    a.on_change = lambda: pushed.append(1)

    check(a.like("aoe3", 7) == 7 and a.like_summary()["aoe3"]["mine"] == 7, "set my hearts")
    check(pushed, "a change is pushed to peers")
    check(a.like("aoe3", 15) == 10, "over the top is 5 full hearts")
    for v in (1, 2, 3, 4, 5, 6):                     # faster than Windows' ~15 ms clock step
        a.like("rapid", v)
    check(a.like_summary()["rapid"]["mine"] == 6, "rapid changes (arrow keys) all count: the last one wins")

    # ---- merging peers' scores ----
    b.like("aoe3", 6)
    b.like("hexen2", 9)
    a.merge(b.state())
    s = a.like_summary()
    check(s["aoe3"]["n"] == 2 and s["aoe3"]["avg"] == 8.0 and s["aoe3"]["mine"] == 10, "average over both players")
    check(s["hexen2"]["mine"] == 0 and s["hexen2"]["avg"] == 9 and s["hexen2"]["voters"] == ["Bob"], "peer-only game")
    old = dict(b.state())
    time.sleep(0.01)
    b.like("aoe3", 2)
    a.merge(b.state())
    a.merge(old)                       # an older copy arriving late doesn't undo the change
    check(a.like_summary()["aoe3"]["avg"] == 6.0, "newest score wins, a late old copy is ignored")
    b.like("hexen2", 0)
    a.merge(b.state())
    check(a.like_summary()["hexen2"]["n"] == 0 and a.like_summary()["hexen2"]["avg"] == 0,
          "0 hearts doesn't count as a vote")
    check(not a.merge({"polls": [], "ballots": []}), "a state from an older launcher (no likes) merges fine")
    check(not a.merge({"likes": [{"voter": "x"}, "junk", {"game": "g"}, {"voter": "y", "game": "g", "ts": "bad"}]}),
          "malformed entries ignored")
    a.merge({"likes": [{"voter": "z" * 500, "game": "g" * 500, "score": 4, "ts": time.time(), "voter_name": "n" * 500}]})
    long = [l for l in a.state()["likes"] if l["voter"].startswith("z")][0]
    check(len(long["voter"]) == 40 and len(long["game"]) == 80 and len(long["voter_name"]) == 40, "lengths capped")

    # ---- persistence ----
    check(os.path.isfile(os.path.join(a_dir, "likes.json")), "saved to likes.json")
    a2 = Votes(Cfg(a_dir, "alice", "Alice"))
    check(a2.like_summary()["aoe3"]["mine"] == 10 and a2.like_summary()["aoe3"]["n"] == 2, "reloaded after a restart")
    with open(os.path.join(a_dir, "likes.json"), "w") as f:
        f.write("{broken")
    check(Votes(Cfg(a_dir, "alice", "Alice")).like_summary() == {}, "a broken file starts empty, no crash")
    with open(os.path.join(a_dir, "likes.json"), "w") as f:
        json.dump([{"voter": "alice", "game": "doom", "score": 99, "ts": 1}], f)
    check(Votes(Cfg(a_dir, "alice", "Alice")).like_summary()["doom"]["mine"] == 10, "loaded scores are clamped too")
    check("likes" in a.state() and "polls" in a.state(), "likes travel in the poll state (/peer/votes)")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
