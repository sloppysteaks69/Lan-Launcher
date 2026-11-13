"""Party Master (v1.8.0): heart scores averaged out of 10 (unscored players and games not counted), only the Party
Master starts polls (locally and as seen by peers), and a poll holds only the games the Party Master ticked.
Run: python tests\\test_partymaster.py"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty.config import Config  # noqa: E402
from lanparty.discovery import Discovery  # noqa: E402
from lanparty.library import Library  # noqa: E402
from lanparty.netwrap import NetWrap  # noqa: E402
from lanparty.peers import PeerSync  # noqa: E402
from lanparty.server import App, Handler  # noqa: E402
from lanparty.transfer import Downloads  # noqa: E402
from lanparty.vote import Votes  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Cfg:
    def __init__(self, d, pid, name, master=False):
        self.data_dir = d
        self.data = {"peer_id": pid, "name": name, "party_master": master}

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)


def hello(pid, name, pm):
    return {"app": "lanparty", "id": pid, "name": name, "port": 1, "v": "1.8.0", "pm": pm}


tmp = tempfile.mkdtemp(prefix="lp_pm_")
try:
    # ---- heart score averages ----
    os.makedirs(os.path.join(tmp, "v"))
    v = Votes(Cfg(os.path.join(tmp, "v"), "me", "Me"))
    now = time.time()
    v.merge({"likes": [
        {"voter": "me", "voter_name": "Me", "game": "aoe3", "score": 8, "ts": now},
        {"voter": "bob", "voter_name": "Bob", "game": "aoe3", "score": 5, "ts": now},
        {"voter": "cat", "voter_name": "Cat", "game": "aoe3", "score": 0, "ts": now},    # 0 = no score
        {"voter": "bob", "voter_name": "Bob", "game": "hexen2", "score": 10, "ts": now},
        {"voter": "cat", "voter_name": "Cat", "game": "doom", "score": 0, "ts": now},     # nobody really scored
    ]})
    titles = {"aoe3": "Age of Empires III", "hexen2": "Hexen II", "doom": "Doom", "quake": "Quake"}
    sb = v.scoreboard(titles)
    by = {g["id"]: g for g in sb["games"]}
    check(by["aoe3"]["avg"] == 6.5 and by["aoe3"]["n"] == 2, "average over the players who scored it (8, 5; Cat's 0 left out)")
    check(by["aoe3"]["voters"] == ["Bob", "Me"], "voters listed")
    check(by["hexen2"]["avg"] == 10 and by["hexen2"]["n"] == 1, "a game one player scored")
    check("doom" not in by and "quake" not in by, "games nobody scored aren't listed (no score, not 0)")
    check(sb["overall"] == 8.2 and sb["scored"] == 2, f"overall average over scored games only ({sb['overall']})")
    check(sb["unscored"] == 2, "two library games without a score")
    check([g["id"] for g in sb["games"]] == ["hexen2", "aoe3"], "best score first")
    check(v.scoreboard({"quake": "Quake"}) == {"games": [], "overall": None, "scored": 0, "unscored": 1},
          "nothing scored: no overall average")
    check(v.scoreboard({"hexen2": "Hexen II"})["games"][0]["title"] == "Hexen II", "only games in the library")
    check(v.like_summary()["doom"]["n"] == 0, "card summary: unscored game has n 0 (UI shows 'no score')")

    # ---- peer intake: new polls only from the Party Master ----
    disc = Discovery({"peer_id": "me", "name": "Me", "party_master": False}, 47801)
    disc.seen(hello("pmid", "Paula", True), "10.0.0.2")
    disc.seen(hello("joe", "Joe", False), "10.0.0.3")
    disc.seen({"app": "lanparty", "id": "old", "name": "Old", "port": 1, "v": "1.7.1"}, "10.0.0.4")  # no pm key
    check(disc.is_party_master("pmid") and not disc.is_party_master("joe"), "hello 'pm' flag read")
    check(not disc.is_party_master("old"), "an older launcher (no flag) isn't Party Master")
    check(not disc.is_party_master("nobody"), "an unknown / offline peer isn't Party Master")
    check(disc.hello()["pm"] is False, "our hello carries pm")
    os.makedirs(os.path.join(tmp, "g"))
    g = Votes(Cfg(os.path.join(tmp, "g"), "me", "Me"))
    g.poll_gate = disc.is_party_master
    poll = lambda pid, by: {"id": pid, "title": "Next?", "candidates": ["a", "b"], "created_by": by,  # noqa: E731
                            "creator_name": by, "created_at": now}
    g.merge({"polls": [poll("p1", "joe")]})
    check("p1" not in g.polls, "new poll from a player who isn't Party Master is refused")
    g.merge({"polls": [poll("p0", "old")]})
    check("p0" not in g.polls, "new poll from an older launcher is refused")
    g.merge({"polls": [poll("p2", "pmid")]})
    check("p2" in g.polls, "new poll from the Party Master is taken in")
    g.merge({"ballots": [{"poll": "p2", "voter": "joe", "voter_name": "Joe", "ranking": ["b", "a"], "ts": now}]})
    check(any(b["voter"] == "joe" for b in g.state()["ballots"]), "ballots from anyone on a known poll are taken")
    disc.seen(hello("pmid", "Paula", False), "10.0.0.2")             # Paula stops being Party Master
    g.merge({"polls": [dict(poll("p2", "pmid"), closed=True)]})
    check(g.polls["p2"]["closed"], "a poll we already have stays (and can still close)")
    g.merge({"polls": [poll("p3", "pmid")]})
    check("p3" not in g.polls, "after she turns it off, her new polls are refused")
    disc.seen(hello("joe", "Joe", True), "10.0.0.3")
    g.merge({"polls": [poll("p1", "joe")]})
    check("p1" in g.polls, "a poll refused earlier is taken once its creator is Party Master (next sync)")

    # ---- /api/vote/new through the real handler ----
    data = os.path.join(tmp, "app")
    games = os.path.join(tmp, "games")
    for d in ("Tank Duel", "Space Blaster", "Warcraft II"):
        os.makedirs(os.path.join(games, d))
        shutil.copy(sys.executable, os.path.join(games, d, "game.exe"))
    cfg = Config(data)
    cfg.set("folders", [games])
    lib = Library(cfg)
    lib.rescan()
    adisc = Discovery(cfg, 0)
    votes = Votes(cfg)
    sync = PeerSync(cfg, adisc, lib, votes)
    app = App(cfg, lib, adisc, sync, votes, Downloads(cfg, adisc, lib, sync), NetWrap(cfg))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.app = app
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-LanParty": "1"},
                                     method="GET" if body is None else "POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    ids = sorted(gm["id"] for gm in call("/api/state")["library"])
    check(len(ids) == 3, f"three games in the library ({ids})")
    check(call("/api/state")["me"]["party_master"] is False and call("/api/state")["scores"] is None,
          "not Party Master: no scores in the state")
    r = call("/api/vote/new", {"title": "x", "candidates": ids})
    check(r.get("ok") is False and "Party Master" in r.get("error", "") and not votes.polls,
          "poll creation refused when this PC isn't Party Master")
    check(call("/api/settings", {"party_master": True})["ok"] and cfg.get("party_master") is True,
          "Party Master toggle saved in settings.json")
    with open(os.path.join(data, "settings.json"), encoding="utf-8") as f:
        check(json.load(f).get("party_master") is True, "settings.json has party_master: true")
    check(adisc.hello()["pm"] is True, "our hello now says Party Master")
    st = call("/api/state")
    check(st["me"]["party_master"] and st["scores"] is not None and st["settings"]["party_master"],
          "Party Master: scores and the setting in the state")
    pick = [ids[0], ids[2]]
    r = call("/api/vote/new", {"title": "Two of them", "candidates": pick + ["notagame", ids[0]]})
    check(r.get("ok") is True, "Party Master starts a poll")
    p = votes.polls[r["id"]]
    check(p["candidates"] == pick, f"only the ticked games are in the poll ({p['candidates']})")
    check(p["created_by"] == cfg["peer_id"], "poll carries the creator's peer id")
    check(call("/api/vote/new", {"title": "none", "candidates": []}).get("ok") is False, "an empty pick is refused")
    check(len(call("/api/vote/new", {"title": "all"}).get("id", "")) > 0, "no list = every game (old behaviour)")
    # peer intake over HTTP: a peer that isn't Party Master can't push a new poll in
    req = urllib.request.Request(f"http://127.0.0.1:{port}/peer/votes", method="POST",
                                 data=json.dumps({"polls": [poll("evil", "joe")]}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
    check("evil" not in votes.polls, "/peer/votes: new poll from a non-master peer refused")
    call("/api/settings", {"party_master": False})
    check(call("/api/vote/new", {"title": "x"}).get("ok") is False, "turned off again: refused again")
    check(r["id"] in votes.polls, "our earlier poll is kept")
    srv.shutdown()
    srv.server_close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
