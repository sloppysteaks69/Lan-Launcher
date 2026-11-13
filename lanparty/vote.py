"""Ranked-choice polls shared by every launcher. Every copy merges the same polls and ballots and counts the
same instant runoff, so all screens agree on the winner."""
import json
import os
import threading
import time
import uuid


def irv(candidates: list, ballots: list) -> dict:
    """Instant-runoff count. ballots: lists of candidate ids, best first.

    Returns {winner, rounds:[{counts, eliminated}], borda}. Ties for elimination drop the candidate with the
    fewest first preferences in earlier rounds, then the lowest Borda score, then the last by id."""
    cands = list(dict.fromkeys(candidates))
    n = len(cands)
    borda = {c: 0 for c in cands}
    for b in ballots:
        ranked = [c for c in dict.fromkeys(b) if c in borda]
        for i, c in enumerate(ranked):
            borda[c] += n - i
    remaining = set(cands)
    rounds = []
    history = []
    winner = None
    while remaining:
        counts = {c: 0 for c in remaining}
        active = 0
        for b in ballots:
            for c in b:
                if c in remaining:
                    counts[c] += 1
                    active += 1
                    break
        history.append(counts)
        if active == 0:
            rounds.append({"counts": counts, "eliminated": None})
            break
        top = max(counts.values())
        if top * 2 > active or len(remaining) == 1:
            winner = sorted((c for c in remaining if counts[c] == top), key=lambda c: (-borda[c], c))[0]
            rounds.append({"counts": counts, "eliminated": None})
            break
        low = min(counts.values())
        tied = [c for c in remaining if counts[c] == low]

        def elim_key(c):
            earlier = tuple(h.get(c, 0) for h in history[:-1])
            return (earlier, borda[c], [-ord(ch) for ch in c])
        out = min(tied, key=elim_key)
        remaining.discard(out)
        rounds.append({"counts": counts, "eliminated": out})
    return {"winner": winner, "rounds": rounds, "borda": borda}


LIKE_MAX = 10          # interest score 0-10 = 0-5 hearts in half-heart steps


def clamp_score(v) -> int:
    try:
        return max(0, min(LIKE_MAX, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


class Votes:
    def __init__(self, cfg):
        self.cfg = cfg
        self.polls = {}     # poll id -> {id, title, candidates, created_by, creator_name, created_at, closed}
        self.ballots = {}   # "poll|voter" -> {poll, voter, voter_name, ranking, ts}
        # Hearts: each player's interest in each game, 0-10. Shared with the party in the same state as the polls
        # (/peer/votes, signed in an internet party). Older launchers ignore the "likes" key.
        self.likes = {}     # "voter|game" -> {voter, voter_name, game, score, ts}
        self._lock = threading.Lock()
        self.on_change = None
        # Party Master: poll_gate(created_by) -> bool decides whether a poll we haven't seen yet is taken in (set by
        # server.App: only polls started by the Party Master). None = take every poll (tests, older behaviour).
        # Ballots, hearts and closing polls we already have are not affected.
        self.poll_gate = None
        self._likes_path = os.path.join(getattr(cfg, "data_dir", "") or "", "likes.json") \
            if getattr(cfg, "data_dir", "") else ""
        self._load_likes()

    def _load_likes(self) -> None:
        if not self._likes_path:
            return
        try:
            with open(self._likes_path, encoding="utf-8") as f:
                self.merge({"likes": json.load(f)}, notify=False, save=False)
        except (OSError, ValueError, TypeError):
            pass

    def _save_likes(self) -> None:
        if not self._likes_path:
            return
        try:
            tmp = self._likes_path + ".tmp"
            with self._lock:
                data = list(self.likes.values())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._likes_path)
        except OSError:
            pass

    def state(self) -> dict:
        with self._lock:
            return {"polls": list(self.polls.values()), "ballots": list(self.ballots.values()),
                    "likes": list(self.likes.values())}

    def like(self, game: str, score) -> int:
        """Set this player's hearts for a game. Returns the stored score."""
        s = clamp_score(score)
        me, gid = str(self.cfg["peer_id"])[:40], str(game)[:80]
        with self._lock:
            prev = (self.likes.get(f"{me}|{gid}") or {}).get("ts", 0.0)
        # Windows' clock moves in ~15 ms steps: two changes in one step would share a timestamp and the second
        # would be dropped as "not newer". Always stamp a new score after the previous one.
        ts = max(time.time(), prev + 0.001)
        self.merge({"likes": [{"voter": me, "voter_name": self.cfg["name"], "game": gid, "score": s, "ts": ts}]})
        return s

    def like_summary(self) -> dict:
        """game -> {mine, avg, n, voters}. avg over players who gave the game at least half a heart."""
        me = self.cfg["peer_id"]
        out = {}
        with self._lock:
            items = list(self.likes.values())
        for l in items:
            g = out.setdefault(l["game"], {"mine": 0, "sum": 0, "n": 0, "voters": []})
            if l["voter"] == me:
                g["mine"] = l["score"]
            if l["score"] > 0:
                g["sum"] += l["score"]
                g["n"] += 1
                g["voters"].append(l["voter_name"])
        for g in out.values():
            g["avg"] = round(g.pop("sum") / g["n"], 1) if g["n"] else 0
        return out

    def scoreboard(self, titles: dict = None) -> dict:
        """The Party Master's view of the hearts: each game's average out of 10 over the players who scored it
        (players with no score, or 0, for a game don't count; a game nobody scored isn't listed), best first, and
        the overall average of those game averages. titles: game id -> title for the games to list (the merged
        library); None lists every scored game."""
        games = []
        for gid, l in self.like_summary().items():
            if not l["n"] or (titles is not None and gid not in titles):
                continue
            games.append({"id": gid, "title": (titles or {}).get(gid, gid), "avg": l["avg"], "n": l["n"],
                          "voters": sorted(l["voters"], key=str.lower)})
        games.sort(key=lambda g: (-g["avg"], -g["n"], g["title"].lower()))
        overall = round(sum(g["avg"] for g in games) / len(games), 1) if games else None
        return {"games": games, "overall": overall, "scored": len(games),
                "unscored": max(0, len(titles) - len(games)) if titles is not None else 0}

    def merge(self, st: dict, notify: bool = True, save: bool = True) -> bool:
        changed = False
        likes_changed = False
        with self._lock:
            for l in (st.get("likes") or []) if isinstance(st.get("likes"), list) else []:
                if not isinstance(l, dict) or not l.get("voter") or not l.get("game"):
                    continue
                key = f"{str(l['voter'])[:40]}|{str(l['game'])[:80]}"
                try:
                    ts = float(l.get("ts", 0))
                except (TypeError, ValueError):
                    continue
                cur = self.likes.get(key)
                if cur is None or ts > cur["ts"]:
                    self.likes[key] = {"voter": str(l["voter"])[:40], "voter_name": str(l.get("voter_name", ""))[:40],
                                       "game": str(l["game"])[:80], "score": clamp_score(l.get("score")), "ts": ts}
                    changed = likes_changed = True
            for p in st.get("polls", []):
                pid = str(p.get("id", ""))
                if not pid:
                    continue
                cur = self.polls.get(pid)
                if cur is None:
                    if self.poll_gate is not None and not self.poll_gate(str(p.get("created_by", ""))):
                        continue      # a new poll not started by the Party Master
                    self.polls[pid] = {"id": pid, "title": str(p.get("title", "Next game"))[:80],
                                       "candidates": [str(c) for c in p.get("candidates", [])][:300],
                                       "created_by": str(p.get("created_by", "")),
                                       "creator_name": str(p.get("creator_name", ""))[:40],
                                       "created_at": float(p.get("created_at", 0)),
                                       "closed": bool(p.get("closed", False))}
                    changed = True
                elif p.get("closed") and not cur["closed"]:
                    cur["closed"] = True
                    changed = True
            for b in st.get("ballots", []):
                key = f"{b.get('poll')}|{b.get('voter')}"
                poll = self.polls.get(str(b.get("poll")))
                if not poll:
                    continue
                cur = self.ballots.get(key)
                if cur is None or float(b.get("ts", 0)) > cur["ts"]:
                    if cur is not None and poll["closed"]:
                        continue   # no changing votes after close
                    self.ballots[key] = {"poll": poll["id"], "voter": str(b.get("voter")),
                                         "voter_name": str(b.get("voter_name", ""))[:40],
                                         "ranking": [str(c) for c in b.get("ranking", []) if str(c) in poll["candidates"]],
                                         "ts": float(b.get("ts", 0))}
                    changed = True
        if likes_changed and save:
            self._save_likes()
        if changed and notify and self.on_change:
            self.on_change()
        return changed

    def new_poll(self, title: str, candidates: list) -> str:
        pid = uuid.uuid4().hex[:10]
        self.merge({"polls": [{"id": pid, "title": title or "What do we play next?", "candidates": candidates,
                               "created_by": self.cfg["peer_id"], "creator_name": self.cfg["name"],
                               "created_at": time.time()}]})
        return pid

    def cast(self, poll_id: str, ranking: list) -> None:
        self.merge({"ballots": [{"poll": poll_id, "voter": self.cfg["peer_id"], "voter_name": self.cfg["name"],
                                 "ranking": ranking, "ts": time.time()}]})

    def close(self, poll_id: str) -> None:
        with self._lock:
            p = self.polls.get(poll_id)
            if not p or p["closed"]:
                return
            p["closed"] = True
        if self.on_change:
            self.on_change()

    def results(self) -> list:
        st = self.state()
        out = []
        for p in sorted(st["polls"], key=lambda p: -p["created_at"]):
            bs = [b for b in st["ballots"] if b["poll"] == p["id"]]
            res = irv(p["candidates"], [b["ranking"] for b in bs])
            out.append({**p, "ballots": bs, "result": res})
        return out
