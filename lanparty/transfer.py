"""Copies a game from every player who has it, all at once, and from players who are still copying it (swarm).

The work is cut into units and shared out: big files in 8 MB chunks (byte ranges), small files in bundles (many files
in one reply, so thousands of small files don't cost a round trip each), the rest whole. Each source gets its own
workers on kept-alive HTTP connections, so a faster source simply takes more units. A unit that fails is retried on
another source; a source that keeps failing is dropped. A file a source has in a different size or hash (another
version of the game) is never fetched from it. Big files resume chunk by chunk after an interruption. Every file is
checked against the SHA-256 in the manifest (hashcache.py fills them in); a file that fails is fetched again, from
the full copies only. Chunks and bundles travel LZ4-compressed when both PCs can (compress.py), and are held to the
play-time limit while a game runs here (throttle.py).

Swarm: while a download runs, this PC lists the game in its hello ("dl") and answers /peer/swarm/<id> with the files
and chunks it already has, and /peer/file and /peer/bundle serve them. A downloader polls the players listed as
downloading the same game every few seconds and adds what they have as extra sources, so several players fetching
the same game at once also feed each other. Units are taken in a shuffled order so they fetch different parts."""
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
from collections import Counter, deque

from . import compress, media, throttle
from .peers import get_json, best_ip, _sign_headers
from .regfix import find_reg_files, import_reg_files

CHUNK = 8 << 20             # big files are fetched in byte ranges of this size, from any source
SMALL = 256 << 10           # files below this go in bundles
BUNDLE_BYTES = 8 << 20
BUNDLE_FILES = 256
WORKERS_PER_SOURCE = 6      # parallel streams per source: hides round-trip time on internet links
MAX_WORKERS = 24
SWARM_WORKERS = 3           # streams to a player that is still downloading (it's busy fetching too)
UNIT_TRIES = 8              # attempts per unit across all sources
SOURCE_FAIL_LIMIT = 6       # consecutive failures before a source is dropped from this download
BAD_FILE_TRIES = 3          # times a big file may fail its hash check before the download gives up on it
SWARM_EVERY = 5.0           # seconds between looks at what the other downloaders have
IO = 1 << 20
TIMEOUT = 30
FROM_HEADER = "X-LP-From"   # our peer id on file requests, only so the source's card can name who is copying
MY_ID = ""
PART = ".lanparty-part"
PROGRESS = ".lanparty-progress"


def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .") or "game"


class _NoBundle(Exception):
    """The source is an older launcher without /peer/bundle."""


class _BadFile(Exception):
    """A big file failed its hash check after all its chunks arrived: every chunk is fetched again."""

    def __init__(self, f: dict):
        super().__init__(f"SHA-256 mismatch for {f['p']}")
        self.f = f


class _Unit:
    """A piece of work: kind 'chunk' (one file, one byte range) or 'bundle' (several whole small files)."""

    def __init__(self, kind: str, files: list, eligible: set, offset: int = 0, length: int = 0, index: int = 0):
        self.kind = kind
        self.files = files
        self.eligible = set(eligible)
        self.offset = offset
        self.length = length
        self.index = index
        self.failed: set = set()
        self.tries = 0
        self.last_error = ""

    def nbytes(self) -> int:
        return self.length if self.kind == "chunk" else sum(f["size"] for f in self.files)


class _Conn:
    """One kept-alive HTTP connection to one source. Each request is signed fresh (a retry gets a new nonce)."""

    def __init__(self, ip: str, port: int):
        self.ip, self.port = ip, port
        self.c = None

    def request(self, method: str, path: str, body: bytes = b"", headers: dict = None) -> http.client.HTTPResponse:
        for attempt in (0, 1):     # a kept-alive connection the server has closed fails once: reconnect and resend
            hdrs = dict(headers or {})
            if MY_ID:
                hdrs[FROM_HEADER] = MY_ID
            if body:
                hdrs["Content-Type"] = "application/json"
            hdrs.update(_sign_headers(method, path, body))
            if self.c is None:
                self.c = http.client.HTTPConnection(self.ip, self.port, timeout=TIMEOUT)
            try:
                self.c.request(method, path, body=body or None, headers=hdrs)
                return self.c.getresponse()
            except (http.client.HTTPException, OSError):
                self.close()
                if attempt:
                    raise
        raise OSError("unreachable")

    def close(self) -> None:
        if self.c is not None:
            try:
                self.c.close()
            except Exception:
                pass
            self.c = None


class _FileState:
    """A big file being written chunk by chunk into its .lanparty-part, with done chunks in .lanparty-progress.
    lock also covers finalizing (hash + rename), so a swarm read never sees the part file vanish mid-read."""

    def __init__(self, f: dict, out: str):
        self.f = f
        self.out = out
        self.part = out + PART
        self.prog = out + PROGRESS
        self.n = max(1, math.ceil(f["size"] / CHUNK))
        self.done: set = set()
        self.finished = False
        self.lock = threading.Lock()

    def prepare(self) -> None:
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        try:
            with open(self.prog, "r", encoding="utf-8") as fh:
                p = json.load(fh)
            if p.get("size") == self.f["size"] and p.get("chunk") == CHUNK and \
                    os.path.getsize(self.part) == self.f["size"]:
                self.done = {int(i) for i in p.get("done", []) if 0 <= int(i) < self.n}
        except (OSError, ValueError, TypeError):
            self.done = set()
        if not self.done:
            with open(self.part, "wb") as fh:      # a part file from an older launcher has no chunk map: start over
                fh.truncate(self.f["size"])

    def mark(self, index: int) -> bool:
        """Record a finished chunk; True when the whole file is now done."""
        with self.lock:
            self.done.add(index)
            tmp = self.prog + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"size": self.f["size"], "chunk": CHUNK, "done": sorted(self.done)}, fh)
            os.replace(tmp, self.prog)
            return len(self.done) == self.n

    def reset(self) -> None:
        with self.lock:
            self.done.clear()
            if os.path.exists(self.prog):
                os.remove(self.prog)

    def covers(self, start: int, length: int) -> bool:
        if length <= 0:
            return False
        first, last = start // CHUNK, (start + length - 1) // CHUNK
        return all(i in self.done for i in range(first, last + 1))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(IO), b""):
            h.update(chunk)
    return h.hexdigest()


def _finalize(f: dict, part: str, out: str, hashes=None) -> None:
    if f.get("sha256"):
        if _sha256(part) != f["sha256"]:
            os.remove(part)
            raise ValueError(f"SHA-256 mismatch for {f['p']}")
        os.replace(part, out)
        if hashes is not None:
            hashes.put(out, f["sha256"])     # verified: no need to hash it again for our own manifest
        return
    os.replace(part, out)


class _Active:
    """What a running download already has on disk, for players fetching the same game (swarm)."""

    def __init__(self, gid: str, dest: str, files: list):
        self.gid = gid
        self.dest = dest
        self.files = {f["p"]: f for f in files if "out" in f}
        self.states: dict = {}           # path -> _FileState (big files)
        self._info = (0.0, None)

    @staticmethod
    def _complete(f: dict) -> bool:
        out = f["out"]
        return os.path.isfile(out) and os.path.getsize(out) == f["size"] and not os.path.exists(out + PART)

    def info(self) -> dict:
        """{"chunk", "have": {path: [size, sha]}, "chunks": {path: [size, sha, [indexes]]}}; cached 2 s."""
        at, cached = self._info
        if cached is not None and time.time() - at < 2.0:
            return cached
        have, chunks = {}, {}
        for p, f in list(self.files.items()):
            st = self.states.get(p)
            if st is not None and not st.finished:
                with st.lock:
                    done = sorted(st.done)
                if done:
                    chunks[p] = [f["size"], f.get("sha256", ""), done]
            elif f["size"] > 0 and self._complete(f):
                have[p] = [f["size"], f.get("sha256", "")]
        out = {"chunk": CHUNK, "have": have, "chunks": chunks}
        self._info = (time.time(), out)
        return out

    def reader(self, rel: str):
        """(size, read(start, length) -> bytes or None) for a file we can serve (complete, or some chunks), or None."""
        f = self.files.get(rel)
        if not f or f["size"] <= 0:
            return None
        st = self.states.get(rel)
        if st is None or st.finished:
            if not self._complete(f):
                return None

            def read_out(start, length, out=f["out"]):
                with open(out, "rb") as fh:
                    fh.seek(start)
                    return fh.read(length)
            return f["size"], read_out

        def read_part(start, length):
            with st.lock:
                if st.finished:
                    path = st.out
                elif st.covers(start, length):
                    path = st.part
                else:
                    return None
                with open(path, "rb") as fh:
                    fh.seek(start)
                    return fh.read(length)
        return f["size"], read_part


class Downloads:
    def __init__(self, cfg, discovery, library, sync):
        self.cfg = cfg
        self.discovery = discovery
        self.library = library
        self.sync = sync
        self.jobs = {}    # game id -> status dict
        self.active = {}  # game id -> _Active, while a download runs (served to the swarm)
        self._lock = threading.Lock()
        global MY_ID
        try:
            MY_ID = str(cfg["peer_id"])[:32]
        except (KeyError, TypeError):
            pass

    def list(self) -> list:
        with self._lock:
            return [dict(j) for j in self.jobs.values()]

    def downloading(self) -> list:
        """Game ids we're downloading now (the swarm hint in our hello)."""
        return list(self.active)[:8]

    def swarm(self, gid: str):
        return self.active.get(gid)

    def start(self, gid: str, owners: list) -> str:
        with self._lock:
            j = self.jobs.get(gid)
            if j and j["state"] in ("starting", "downloading"):
                return "already running"
            self.jobs[gid] = {"id": gid, "state": "starting", "done": 0, "total": 0, "files_done": 0,
                              "files": 0, "rate": 0, "wire": 0, "wire_rate": 0, "error": "", "dest": "",
                              "sources": [], "source_stats": [], "started": time.time(), "checked": 0, "throttle": throttle.status()}
        threading.Thread(target=self._run_safe, args=(gid, owners), daemon=True).start()
        return "ok"

    def _upd(self, gid: str, **kw) -> None:
        with self._lock:
            self.jobs[gid].update(kw)

    def _target_root(self) -> str:
        d = self.cfg.get("download_dir") or ""
        if not d and self.cfg["folders"]:
            d = self.cfg["folders"][0]
        if not d:
            d = os.path.join(os.path.expanduser("~"), "LanPartyGames")
        os.makedirs(d, exist_ok=True)
        return d

    def _run_safe(self, gid: str, owners: list) -> None:
        try:
            self._run(gid, owners)
        except Exception as e:      # never leave a job stuck on "downloading"
            self._upd(gid, state="error", error=f"Download stopped: {e}. Click again to resume.")
        finally:
            self.active.pop(gid, None)

    # ---- planning ----
    def _sources(self, gid: str, owners: list) -> tuple:
        """(sources, manifests): each online owner with its fastest address and its file list, fetched in parallel."""
        peers = [p for p in (self.discovery.get(o) for o in owners) if p]
        if not peers:
            return [], {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(peers))) as ex:
            ips = dict(zip([p["id"] for p in peers], ex.map(best_ip, peers)))

            def man(p):
                try:
                    return get_json(p, f"/peer/manifest/{urllib.parse.quote(gid)}", timeout=60, ip=ips[p["id"]])
                except (OSError, ValueError):
                    return None
            mans = dict(zip([p["id"] for p in peers], ex.map(man, peers)))
        srcs = [{"id": p["id"], "name": p["name"], "ip": ips[p["id"]], "port": p["port"], "kind": "full"}
                for p in peers if mans.get(p["id"]) and mans[p["id"]].get("files") is not None]
        return srcs, {s["id"]: mans[s["id"]] for s in srcs}

    @staticmethod
    def _agree(srcs: list, mans: dict) -> tuple:
        """The file list most sources agree on: per path, the most common size, then among the sources with that
        size the most common SHA-256 (first source wins a tie; a source that hasn't hashed the file yet doesn't
        vote). Returns (files, has) where has[source id] = paths that source has in that size (and hash, if known)."""
        sizes, shas = {}, {}
        for s in srcs:
            for f in mans[s["id"]]["files"]:
                sizes.setdefault(f["p"], Counter())[f["size"]] += 1
                if f.get("sha256"):
                    shas.setdefault((f["p"], f["size"]), Counter())[f["sha256"]] += 1
        files = []
        for path, c in sizes.items():
            size = c.most_common(1)[0][0]
            sc = shas.get((path, size))
            files.append({"p": path, "size": size, "sha256": sc.most_common(1)[0][0] if sc else ""})
        truth = {f["p"]: f for f in files}

        def same(f):
            t = truth.get(f["p"])
            return t is not None and t["size"] == f["size"] and \
                (not f.get("sha256") or not t["sha256"] or f["sha256"] == t["sha256"])
        has = {s["id"]: {f["p"] for f in mans[s["id"]]["files"] if same(f)} for s in srcs}
        return files, has

    # ---- the download ----
    def _run(self, gid: str, owners: list) -> None:
        srcs, mans = self._sources(gid, owners)
        if not srcs:
            self._upd(gid, state="error", error="No player who has this game is online, or none answered.")
            return
        first = mans[srcs[0]["id"]]
        files, has = self._agree(srcs, mans)
        cat = media.category_of(gid)        # a movie, album, book...: into that category's folder (media.py)
        single = False
        if cat:
            root = self.library.media.folder(cat)
            if not root:
                self._upd(gid, state="error", error=f"Choose a folder for {media.CATEGORIES[cat]} first "
                                                    f"(Library > {media.CATEGORIES[cat]}, or Settings).")
                return
            os.makedirs(root, exist_ok=True)
            # one file (a movie, a PDF): straight into the folder, as long as every source agrees it's one file
            single = bool(first.get("single")) and len(files) == 1 and "/" not in files[0]["p"]
            dest = root if single else os.path.join(root, _safe_name(first.get("folder") or first.get("title") or gid))
        else:
            root = self._target_root()
            dest = os.path.join(root, _safe_name(first.get("folder") or first.get("title") or gid))
        # abspath+normpath, not realpath: realpath of a folder that doesn't exist yet can come back as \\?\C:\...
        dest_real = os.path.normcase(os.path.normpath(os.path.abspath(dest)))
        total = sum(f["size"] for f in files)
        hashes = getattr(self.library, "hashes", None)

        errors: list = []
        prog = {"bytes": 0, "files": 0, "wire": 0}
        plock = threading.Lock()

        def add(n: int, files_done: int = 0) -> None:
            with plock:
                prog["bytes"] += n
                prog["files"] += files_done

        def add_wire(n: int) -> None:
            with plock:
                prog["wire"] += n

        units, states, small = [], {}, {}
        checked = sum(1 for f in files if f.get("sha256"))
        for f in sorted(files, key=lambda f: -f["size"]):
            out = os.path.normpath(os.path.abspath(os.path.join(dest, f["p"].replace("/", os.sep))))
            if not os.path.normcase(out).startswith(dest_real.rstrip(os.sep) + os.sep):
                errors.append(f"{f['p']}: path outside the game folder")    # a hostile peer
                continue
            f["out"] = out
            elig = {s["id"] for s in srcs if f["p"] in has[s["id"]]}
            if os.path.isfile(out) and os.path.getsize(out) == f["size"] and not os.path.exists(out + PART):
                add(f["size"], 1)
                continue
            if f["size"] == 0:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                open(out, "wb").close()
                add(0, 1)
                continue
            if not elig:
                errors.append(f"{f['p']}: no online player has this file in the agreed size")
                continue
            if f["size"] < SMALL:
                small.setdefault(frozenset(elig), []).append(f)
                continue
            st = _FileState(f, out)
            st.prepare()
            states[f["p"]] = st
            add(len(st.done) * CHUNK - (CHUNK * st.n - f["size"] if (st.n - 1) in st.done else 0))
            if len(st.done) == st.n:
                with st.lock:
                    _finalize(f, st.part, out, hashes)
                    st.finished = True
                os.remove(st.prog)
                add(0, 1)
                continue
            for i in range(st.n):
                if i not in st.done:
                    off = i * CHUNK
                    units.append(_Unit("chunk", [f], elig, off, min(CHUNK, f["size"] - off), i))
        for elig, group in small.items():       # bundle files that the same sources have
            batch, size = [], 0
            for f in group:
                if batch and (len(batch) >= BUNDLE_FILES or size + f["size"] > BUNDLE_BYTES):
                    units.append(_Unit("bundle", batch, elig))
                    batch, size = [], 0
                batch.append(f)
                size += f["size"]
            if batch:
                units.append(_Unit("bundle", batch, elig))
        random.shuffle(units)     # players downloading the same game at once start on different parts (swarm)

        act = _Active(gid, dest, files)
        act.states = states
        self.active[gid] = act
        full_ids = {s["id"] for s in srcs}
        self._upd(gid, state="downloading", total=total, files=len(files), dest=dest, checked=checked,
                  sources=[s["name"] for s in srcs], resumed=prog["bytes"])   # resumed: already here at the start

        # ---- scheduler: workers pull the next unit their source can serve ----
        pending = deque(units)
        cond = threading.Condition()
        sched = {"in_flight": 0}
        dropped: set = set()
        no_bundle: set = set()
        bad_files: Counter = Counter()
        stats = {s["id"]: {"id": s["id"], "name": s["name"], "bytes": 0, "wire": 0, "fails": 0, "state": "ok",
                           "kind": "full", "rate": 0} for s in srcs}

        def usable(u: _Unit) -> set:
            return u.eligible - u.failed - dropped

        def give_up(u: _Unit) -> None:
            errors.append(f"{u.files[0]['p']}: {u.last_error or 'no source left'}")

        def take(sid: str):
            with cond:
                while True:
                    if sid in dropped:
                        return None
                    for _ in range(len(pending)):
                        u = pending.popleft()
                        if sid in usable(u):
                            if u.kind == "bundle" and sid in no_bundle:      # older peer: one file at a time
                                pending.extendleft(_Unit("chunk", [f], u.eligible, 0, f["size"]) for f in u.files)
                                continue
                            sched["in_flight"] += 1
                            return u
                        if not usable(u):
                            give_up(u)
                        else:
                            pending.append(u)
                    # Stay until everything is done: a unit that fails on another source may come back for us.
                    # (Every pending unit has a live source, and a live source's workers never leave early.)
                    if not pending and sched["in_flight"] == 0:
                        return None
                    cond.wait(1.0)

        def finish(u: _Unit, ok: bool, sid: str, err: str = "") -> None:
            with cond:
                sched["in_flight"] -= 1
                if not ok:
                    u.tries += 1
                    u.failed.add(sid)
                    u.last_error = err
                    if u.tries < UNIT_TRIES and usable(u):
                        pending.appendleft(u)
                    elif u.tries < UNIT_TRIES and u.eligible - dropped:
                        u.failed.clear()       # every live source failed it once: give them another go
                        pending.append(u)
                    else:
                        give_up(u)
                cond.notify_all()

        def requeue(new_units: list) -> None:
            with cond:
                pending.extendleft(new_units)
                cond.notify_all()

        def refetch_file(u: _Unit, f: dict) -> None:
            """A big file failed its hash: fetch every chunk again, from the full copies if any still has it."""
            with cond:
                sched["in_flight"] -= 1
                bad_files[f["p"]] += 1
                if bad_files[f["p"]] >= BAD_FILE_TRIES:
                    errors.append(f"{f['p']}: SHA-256 mismatch {bad_files[f['p']]} times")
                else:
                    elig = (u.eligible & full_ids) - dropped or u.eligible
                    st = states[f["p"]]
                    pending.extend(_Unit("chunk", [f], elig, i * CHUNK, min(CHUNK, f["size"] - i * CHUNK), i)
                                   for i in range(st.n))
                cond.notify_all()

        def worker(src: dict) -> None:
            sid = src["id"]
            conn = _Conn(src["ip"], src["port"])
            try:
                while True:
                    u = take(sid)
                    if u is None:
                        return
                    try:
                        if u.kind == "chunk":
                            n, w = self._fetch_chunk(conn, gid, u, states, add, hashes)
                        else:
                            n, w, redo = self._fetch_bundle(conn, gid, u, add, hashes)
                            if redo:
                                requeue([_Unit("chunk", [f], u.eligible - {sid}, 0, f["size"]) for f in redo])
                        stats[sid]["bytes"] += n
                        stats[sid]["wire"] += w
                        add_wire(w)
                        stats[sid]["fails"] = 0
                        finish(u, True, sid)
                    except _NoBundle:
                        conn.close()
                        with cond:
                            no_bundle.add(sid)
                            sched["in_flight"] -= 1
                            pending.extendleft(_Unit("chunk", [f], u.eligible, 0, f["size"]) for f in u.files)
                            cond.notify_all()
                    except _BadFile as e:
                        refetch_file(u, e.f)
                    except (OSError, ValueError, http.client.HTTPException) as e:
                        conn.close()
                        stats[sid]["fails"] += 1
                        if stats[sid]["fails"] >= SOURCE_FAIL_LIMIT:
                            with cond:
                                dropped.add(sid)
                                stats[sid]["state"] = f"dropped: {e}"
                        finish(u, False, sid, str(e))
                        time.sleep(0.5)
            finally:
                conn.close()

        n_each = max(1, min(WORKERS_PER_SOURCE, MAX_WORKERS // len(srcs)))
        # interleaved, so the first source's workers don't grab everything before the others start
        threads = [threading.Thread(target=worker, args=(s,), daemon=True) for _ in range(n_each) for s in srcs]
        for t in threads:
            t.start()

        # ---- swarm: other players downloading this game serve what they already have ----
        swarm_srcs: dict = {}     # peer id -> source dict

        def swarm_look() -> None:
            truth = {f["p"]: f for f in files if "out" in f}
            for p in self.discovery.list():
                pid = p["id"]
                if pid in full_ids or pid in dropped or gid not in (p.get("dl") or []):
                    continue
                try:
                    ip = swarm_srcs[pid]["ip"] if pid in swarm_srcs else best_ip(p)
                    info = get_json(p, f"/peer/swarm/{urllib.parse.quote(gid)}", timeout=10, ip=ip)
                except (OSError, ValueError):
                    continue
                if not isinstance(info, dict) or info.get("chunk") != CHUNK:
                    continue

                def ok(path, size, sha):
                    t = truth.get(path)
                    return t is not None and t["size"] == size and (not sha or not t["sha256"] or sha == t["sha256"])
                whole = {path for path, v in (info.get("have") or {}).items()
                         if isinstance(v, list) and len(v) == 2 and ok(path, v[0], v[1])}
                parts = {}
                for path, v in (info.get("chunks") or {}).items():
                    if isinstance(v, list) and len(v) == 3 and ok(path, v[0], v[1]) and isinstance(v[2], list):
                        parts[path] = {i for i in v[2] if isinstance(i, int)}
                if not whole and not parts:
                    continue
                added = 0
                with cond:
                    for u in pending:
                        if pid in u.eligible:
                            continue
                        if u.kind == "chunk":
                            path = u.files[0]["p"]
                            if path in whole or (u.length == min(CHUNK, u.files[0]["size"] - u.offset)
                                                 and u.offset % CHUNK == 0 and u.offset // CHUNK in parts.get(path, ())):
                                u.eligible.add(pid)
                                added += 1
                        elif all(f["p"] in whole for f in u.files):
                            u.eligible.add(pid)
                            added += 1
                    if added:
                        cond.notify_all()
                if added and pid not in swarm_srcs:
                    src = {"id": pid, "name": p["name"], "ip": ip, "port": p["port"], "kind": "swarm"}
                    swarm_srcs[pid] = src
                    stats[pid] = {"id": pid, "name": p["name"], "bytes": 0, "wire": 0, "fails": 0, "state": "ok",
                                  "kind": "swarm", "rate": 0}
                    for _ in range(SWARM_WORKERS):
                        t = threading.Thread(target=worker, args=(src,), daemon=True)
                        threads.append(t)
                        t.start()

        swarm_busy = threading.Lock()

        def swarm_bg() -> None:
            if not swarm_busy.acquire(blocking=False):
                return
            try:
                swarm_look()
            except Exception:
                pass
            finally:
                swarm_busy.release()

        window = deque([(time.time(), prog["bytes"], prog["wire"], {})])
        last_swarm = 0.0
        while any(t.is_alive() for t in list(threads)):
            time.sleep(0.5)
            now = time.time()
            if now - last_swarm >= SWARM_EVERY:
                last_swarm = now
                threading.Thread(target=swarm_bg, daemon=True).start()
            per = {sid: v["bytes"] for sid, v in list(stats.items())}
            window.append((now, prog["bytes"], prog["wire"], per))
            while len(window) > 2 and now - window[0][0] > 5:     # rate over the last ~5 s, not since the start
                window.popleft()
            t0, b0, w0, per0 = window[0]
            dt = max(now - t0, 0.001)
            for sid, v in list(stats.items()):
                v["rate"] = int(max(0, v["bytes"] - per0.get(sid, 0)) / dt)
            self._upd(gid, done=prog["bytes"], files_done=prog["files"],
                      rate=int(max(0, prog["bytes"] - b0) / dt), wire=prog["wire"],
                      wire_rate=int(max(0, prog["wire"] - w0) / dt), throttle=throttle.status(),
                      source_stats=[dict(v) for v in list(stats.values())])
        with cond:
            for u in pending:
                give_up(u)
            pending.clear()
        self._upd(gid, done=prog["bytes"], files_done=prog["files"], rate=0, wire_rate=0, wire=prog["wire"],
                  source_stats=[dict(v, rate=0) for v in list(stats.values())])
        if errors:
            self._upd(gid, state="error", error=f"{len(errors)} file(s) failed, e.g. {errors[0]}. Click again to resume.")
            return
        self.active.pop(gid, None)
        if cat:
            if not single:      # a single file's folder is the whole category folder: others may be resuming there
                self._sweep(dest)
            self.library.media.rescan()
            self.sync.refresh_libraries_soon()
            self._upd(gid, state="done")
            return
        self._sweep(dest)
        if root not in self.cfg["folders"]:
            self.cfg.set("folders", self.cfg["folders"] + [root])
        self.library.rescan()
        self.sync.refresh_libraries_soon()
        # auto-import .reg files (registry keys with paths rewritten to this PC)
        if find_reg_files(dest):
            game = {"path": dest, "id": gid}
            import_reg_files(self.cfg, game, lambda m: self._upd(gid, error=m))
        self._upd(gid, state="done")

    @staticmethod
    def _sweep(dest: str) -> int:
        """After a download finished with every file in place, any part/progress file left in the game folder is
        stale (an older launcher, an abandoned attempt, a file the sources no longer have): delete it.
        A download that ends in an error keeps them, so clicking again resumes."""
        n = 0
        for root, _, fns in os.walk(dest):
            for fn in fns:
                if fn.endswith((PART, PROGRESS, PROGRESS + ".tmp")):
                    try:
                        os.remove(os.path.join(root, fn))
                        n += 1
                    except OSError:
                        pass
        return n

    # ---- fetching ----
    @staticmethod
    def _read_into(resp, fh, n: int, add, limit: bool = True) -> int:
        """Copy exactly n bytes from resp to fh, counting progress as it goes (and keeping to the play-time limit
        when the bytes come straight off the network)."""
        got = 0
        while got < n:
            want = min(IO, n - got)
            if limit:
                throttle.DOWN.take(want)
            chunk = resp.read(want)
            if not chunk:
                break
            fh.write(chunk)
            add(len(chunk))
            got += len(chunk)
        if got != n:
            add(-got)
            raise ValueError(f"short read ({got} of {n} bytes)")
        return got

    def _fetch_chunk(self, conn: _Conn, gid: str, u: _Unit, states: dict, add, hashes=None) -> tuple:
        """Returns (bytes written, bytes on the wire)."""
        f = u.files[0]
        path = f"/peer/file/{urllib.parse.quote(gid)}?p={urllib.parse.quote(f['p'])}"
        end = u.offset + u.length - 1
        resp = conn.request("GET", path, headers={"Range": f"bytes={u.offset}-{end}", **compress.request_headers()})
        if resp.status not in (200, 206):
            resp.read()
            raise ValueError(f"HTTP {resp.status}")
        resp = compress.unwrap(resp, take=throttle.DOWN.take)   # an lz4 reply is unpacked in memory
        packed = isinstance(resp, compress.Unpacked)
        start = 0
        if resp.status == 206:
            m = re.match(r"bytes (\d+)-", resp.getheader("Content-Range", ""))
            start = int(m.group(1)) if m else -1
        if start != u.offset:
            conn.close()
            raise ValueError("source ignored the byte range")
        sent = int(resp.getheader("Content-Length", "-1"))
        st = states.get(f["p"])
        if st is None:                  # a small file split out of a bundle: write it whole
            part = f["out"] + PART
            os.makedirs(os.path.dirname(part), exist_ok=True)
            with open(part, "wb") as fh:
                n = self._read_into(resp, fh, u.length, add, not packed)
            if sent != u.length:
                conn.close()            # an older launcher sent more than we asked for: this connection is spent
            try:
                _finalize(f, part, f["out"], hashes)
            except ValueError:
                add(-n)
                raise
            add(0, 1)
            return n, resp.wire_bytes if packed else n
        with open(st.part, "r+b") as fh:
            fh.seek(u.offset)
            n = self._read_into(resp, fh, u.length, add, not packed)
        if sent != u.length:
            conn.close()                # an older launcher ignores the range end and sends to the end of the file
        wire = resp.wire_bytes if packed else n
        if st.mark(u.index):
            try:
                with st.lock:
                    _finalize(f, st.part, st.out, hashes)
                    st.finished = True
            except ValueError:
                add(-f["size"])
                st.reset()
                st.prepare()            # the bad part file was removed: start a fresh one for the refetch
                raise _BadFile(f)
            except OSError:
                st.reset()
                add(-f["size"])
                raise
            os.remove(st.prog)
            add(0, 1)
        return n, wire

    def _fetch_bundle(self, conn: _Conn, gid: str, u: _Unit, add, hashes=None) -> tuple:
        """Fetch several small files in one reply. Returns (bytes, wire bytes, files to fetch again elsewhere)."""
        # a retry of a bundle that broke halfway: the files that already arrived are done (and counted)
        u.files = [f for f in u.files if not (os.path.isfile(f["out"]) and os.path.getsize(f["out"]) == f["size"])]
        if not u.files:
            return 0, 0, []
        body = json.dumps({"files": [f["p"] for f in u.files]}).encode("utf-8")
        resp = conn.request("POST", f"/peer/bundle/{urllib.parse.quote(gid)}", body=body,
                            headers=compress.request_headers())
        if resp.status == 404:
            resp.read()
            raise _NoBundle()
        if resp.status != 200:
            resp.read()
            raise ValueError(f"HTTP {resp.status}")
        resp = compress.unwrap(resp, take=throttle.DOWN.take)
        packed = isinstance(resp, compress.Unpacked)
        head = json.loads(resp.readline().decode("utf-8"))
        sizes = head.get("sizes", [])
        if len(sizes) != len(u.files):
            conn.close()
            raise ValueError("bad bundle header")
        total, redo, wire = 0, [], 0
        for f, size in zip(u.files, sizes):
            if size != f["size"]:
                if size > 0:            # a different version of the file: skip its bytes, get it from someone else
                    left = size
                    while left > 0:
                        if not packed:
                            throttle.DOWN.take(min(IO, left))
                        c = resp.read(min(IO, left))
                        if not c:
                            raise ValueError("short read")
                        left -= len(c)
                        wire += len(c)
                redo.append(f)
                continue
            part = f["out"] + PART
            os.makedirs(os.path.dirname(part), exist_ok=True)
            with open(part, "wb") as fh:
                n = self._read_into(resp, fh, size, add, not packed)
            wire += n
            try:
                _finalize(f, part, f["out"], hashes)
            except ValueError:
                add(-n)
                redo.append(f)
                continue
            add(0, 1)
            total += n
        return total, (resp.wire_bytes if packed else wire), redo


class Uploads:
    """Bytes we've sent to each peer per game, with a rate over the last few seconds (for the game cards)."""

    WINDOW = 5.0
    SHOW_FOR = 8.0      # an upload stays on the card this long after its last byte

    def __init__(self, discovery=None):
        self.discovery = discovery
        self._lock = threading.Lock()
        self.games: dict = {}     # gid -> {ip -> {"bytes", "wire", "hits": deque[(t, raw)], "last"}}

    def add(self, gid: str, ip: str, wire: int, raw: int, pid: str = "") -> None:
        now = time.time()
        key = (pid[:32], ip)
        with self._lock:
            e = self.games.setdefault(gid, {}).setdefault(key, {"bytes": 0, "wire": 0, "hits": deque(), "last": 0})
            e["bytes"] += raw
            e["wire"] += wire
            e["last"] = now
            e["hits"].append((now, raw))
            while e["hits"] and now - e["hits"][0][0] > self.WINDOW:
                e["hits"].popleft()

    def _name(self, key: tuple) -> str:
        pid, ip = key
        try:
            peers = self.discovery.list()
            for p in peers:
                if pid and p.get("id") == pid:
                    return p["name"]
            for p in peers:
                if not pid and (ip == p.get("ip") or ip in (p.get("ips") or [])):
                    return p["name"]
        except Exception:
            pass
        return ip

    def list(self) -> list:
        """[{id, rate, bytes, wire, peers: [{name, rate, bytes, wire}]}] for games uploaded in the last few seconds."""
        now = time.time()
        out = []
        with self._lock:
            for gid, by_ip in list(self.games.items()):
                peers = []
                for ip, e in list(by_ip.items()):
                    if now - e["last"] > self.SHOW_FOR:
                        continue
                    recent = sum(n for t, n in e["hits"] if now - t <= self.WINDOW)
                    peers.append({"level": compress.ADAPT.levels.get(ip[1], 0), "key": ip, "rate": int(recent / self.WINDOW), "bytes": e["bytes"], "wire": e["wire"]})
                if peers:
                    out.append({"id": gid, "peers": peers, "rate": sum(p["rate"] for p in peers),
                                "bytes": sum(p["bytes"] for p in peers), "wire": sum(p["wire"] for p in peers)})
        for u in out:
            for p in u["peers"]:
                p["name"] = self._name(p.pop("key"))
        return out
