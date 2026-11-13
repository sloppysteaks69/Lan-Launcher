"""Swarm sharing, always-verify hashes and the play-time limit (v1.10.0), against real launcher HTTP servers on this
PC with party signing on.
  - swarm: A has the game and is slow. X starts downloading it; Y starts a little later and must get part of it
    from X (still downloading), and both end byte-identical. /peer/swarm and serving half-done files are checked.
  - verify: a source that sends one corrupted chunk the first time: the file fails its SHA-256, every chunk is
    fetched again, and the download ends correct. HashCache: put/get, a changed file is forgotten, background pass,
    Library manifests carry the hashes.
  - play-time limit: with a game running, a 12 MB copy at 3 MB/s takes about 4 s; without, it's quick. The game
    watcher spots a program running from a game folder.
  - uploads: the source lists who is copying what, with bytes and rate.
Run: python tests\\test_swarm.py"""
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import hashcache, peers, security, throttle, transfer  # noqa: E402
from lanparty.server import Handler, Server as ThreadingHTTPServer  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


GID = "swarmgame"
SECRET = security.make_party_secret()
PARTY = "sparty"
transfer.SWARM_EVERY = 0.5


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha(path):
    with open(path, "rb") as f:
        return sha_bytes(f.read())


class FakeLibrary:
    def __init__(self, path, hashed=True):
        self.path = path
        self.hashed = hashed

    def has(self, gid):
        return self.get(gid) is not None

    def get(self, gid):
        return {"path": self.path, "title": "Swarm Game", "folder": "Swarm Game"} if gid == GID and self.path else None

    def manifest(self, gid):
        if not self.get(gid):
            return None
        out = []
        for root, _, files in os.walk(self.path):
            for fn in files:
                full = os.path.join(root, fn)
                e = {"p": os.path.relpath(full, self.path).replace("\\", "/"), "size": os.path.getsize(full)}
                if self.hashed:
                    e["sha256"] = sha(full)
                out.append(e)
        return {"id": GID, "title": "Swarm Game", "folder": "Swarm Game", "files": out}

    def resolve_file(self, gid, rel):
        if not self.get(gid):
            return None
        base = os.path.realpath(self.path)
        full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if os.path.commonpath([base, full]) != base or not os.path.isfile(full):
            return None
        return full

    # what Downloads needs after a download
    def rescan(self):
        pass


class FakeDiscovery:
    def __init__(self, pid, peer_list=lambda: []):
        self.pid = pid
        self.peer_list = peer_list

    def hello(self):
        return {"app": "lanparty", "id": self.pid, "name": self.pid}

    def list(self):
        return self.peer_list()

    def get(self, pid):
        return next((p for p in self.peer_list() if p["id"] == pid), None)


class Cfg(dict):
    def set(self, k, v):
        self[k] = v


class Sync:
    def refresh_libraries_soon(self):
        pass


class FakeApp:
    def __init__(self, pid, path, downloads=None, peer_list=lambda: []):
        self.cfg = {"share_library": True}
        self.library = FakeLibrary(path)
        self.discovery = FakeDiscovery(pid, peer_list)
        self.auth = security.PartyAuth(PARTY, SECRET)
        self.downloads = downloads
        self.uploads = transfer.Uploads(self.discovery)


class SlowHandler(Handler):
    """A full copy on a slow link: every file request waits a bit."""

    def _serve_range(self, size, read, names, gid=""):
        time.sleep(1.5)
        return super()._serve_range(size, read, names, gid)


def serve(app, handler=Handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    httpd.app = app
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def wait(dls, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = dls.list()[0]
        if j["state"] in ("done", "error"):
            return j
        time.sleep(0.1)
    return dls.list()[0]


def same_tree(a, b):
    bad = []
    for root, _, files in os.walk(a):
        for fn in files:
            src = os.path.join(root, fn)
            dst = os.path.join(b, os.path.relpath(src, a))
            if not os.path.isfile(dst) or sha(dst) != sha(src):
                bad.append(os.path.relpath(src, a))
    left = [f for _, _, fs in os.walk(b) for f in fs if f.endswith((transfer.PART, transfer.PROGRESS))]
    return bad, left


peers.set_auth(PARTY, security.derive_player_key(SECRET, "me"), "me")
tmp = tempfile.mkdtemp(prefix="lp_swarm_")
a_dir = os.path.join(tmp, "A")
rnd = random.Random(5)
write(os.path.join(a_dir, "big.pak"), os.urandom(96 * (1 << 20)))          # 12 chunks
write(os.path.join(a_dir, "second.pak"), os.urandom(40 * (1 << 20) + 7))   # 5 chunks
for i in range(120):
    write(os.path.join(a_dir, "data", f"f{i}.dat"), os.urandom(rnd.randint(1, 50 * 1024)))

# ---- swarm ----
PEERS = {}
sa = serve(FakeApp("A", a_dir), SlowHandler)
PEERS["A"] = {"id": "A", "name": "A", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sa.server_address[1], "dl": []}


def plist():
    out = []
    for pid, p in PEERS.items():
        p = dict(p)
        if pid in DL:
            p["dl"] = DL[pid].downloading()
        out.append(p)
    return out


DL = {}
for name in ("X", "Y"):
    d = os.path.join(tmp, "dl" + name)
    DL[name] = transfer.Downloads(Cfg(download_dir=d, folders=[]), FakeDiscovery(name, lambda: plist()),
                                  FakeLibrary(""), Sync())
    srv = serve(FakeApp(name, "", DL[name], plist))
    PEERS[name] = {"id": name, "name": name, "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": srv.server_address[1],
                   "dl": []}

# Y must not look at itself as a swarm source: its own list hides it
DL["Y"].discovery = FakeDiscovery("Y", lambda: [p for p in plist() if p["id"] != "Y"])
DL["X"].discovery = FakeDiscovery("X", lambda: [p for p in plist() if p["id"] != "X"])

t0 = time.time()
DL["X"].start(GID, ["A"])
time.sleep(2.5)
check(DL["X"].downloading() == [GID], "X lists the game as downloading (its hello's swarm hint)")
c = transfer._Conn("127.0.0.1", PEERS["X"]["port"])
r = c.request("GET", f"/peer/swarm/{GID}")
import json  # noqa: E402
info = json.loads(r.read())
c.close()
check(r.status == 200 and info.get("chunk") == transfer.CHUNK and (info["have"] or info["chunks"]),
      f"X's /peer/swarm lists what it has so far ({len(info.get('have', {}))} files, "
      f"{sum(len(v[2]) for v in info.get('chunks', {}).values())} chunks)")
if info.get("chunks"):
    path, (size, _, idx) = next(iter(info["chunks"].items()))
    c = transfer._Conn("127.0.0.1", PEERS["X"]["port"])
    r = c.request("GET", f"/peer/file/{GID}?p={path}",
                  headers={"Range": f"bytes={idx[0] * transfer.CHUNK}-{idx[0] * transfer.CHUNK + 1023}"})
    got = r.read()
    with open(os.path.join(a_dir, path), "rb") as f:
        f.seek(idx[0] * transfer.CHUNK)
        want = f.read(1024)
    check(r.status == 206 and got == want, f"X serves a done chunk of a half-downloaded file ({r.status})")
    missing = next((i for i in range(size // transfer.CHUNK) if i not in idx), None)
    if missing is not None:
        r = c.request("GET", f"/peer/file/{GID}?p={path}",
                      headers={"Range": f"bytes={missing * transfer.CHUNK}-{missing * transfer.CHUNK + 1023}"})
        r.read()
        check(r.status == 404, f"X refuses a chunk it doesn't have yet ({r.status})")
    c.close()
DL["Y"].start(GID, ["A"])
jx, jy = wait(DL["X"]), wait(DL["Y"])
check(jx["state"] == "done", f"X finished ({jx['state']} {jx['error']})")
check(jy["state"] == "done", f"Y finished ({jy['state']} {jy['error']}) in {time.time() - t0:.1f}s")
from_x = sum(s["bytes"] for s in jy["source_stats"] if s["id"] == "X")
kinds = {s["id"]: s["kind"] for s in jy["source_stats"]}
check(from_x > 0 and kinds.get("X") == "swarm",
      f"Y got part of the game from X while X was still downloading ({from_x >> 20} MB, {kinds})")
for name in ("X", "Y"):
    bad, left = same_tree(a_dir, os.path.join(tmp, "dl" + name, "Swarm Game"))
    check(not bad and not left, f"{name}: every file identical, no part files left ({bad[:3]} {left[:3]})")
check(jy["checked"] == len(os.listdir(os.path.join(a_dir, "data"))) + 2, f"every file had a hash to check ({jy['checked']})")
check(DL["X"].downloading() == [] and DL["Y"].downloading() == [], "nobody lists the game as downloading after")
check(jy["wire"] > 0 and jy["total"] == jy["done"], "wire bytes and progress counted")
ups = sa.app.uploads.list()
check(ups and ups[0]["id"] == GID and {p["name"] for p in ups[0]["peers"]} == {"127.0.0.1"} or
      (ups and ups[0]["bytes"] > 0), f"A lists its uploads for the card ({ups[0]['bytes'] >> 20 if ups else 0} MB)")

# ---- stale part files are swept after a finished download ----
dw = os.path.join(tmp, "dlW")
stale = os.path.join(dw, "Swarm Game", "old", "gone.pak" + transfer.PART)
write(stale, os.urandom(1000))
write(os.path.join(dw, "Swarm Game", "old", "gone.pak" + transfer.PROGRESS), b"{}")
write(os.path.join(dw, "Swarm Game", "big.pak" + transfer.PART), os.urandom(1000))   # wrong size: restarted
PW = {"A2": {"id": "A2", "name": "A2", "ip": "127.0.0.1", "ips": ["127.0.0.1"],
             "port": serve(FakeApp("A2", a_dir)).server_address[1]}}
dlw = transfer.Downloads(Cfg(download_dir=dw, folders=[]), FakeDiscovery("W", lambda: list(PW.values())),
                         FakeLibrary(""), Sync())
dlw.start(GID, ["A2"])
jw = wait(dlw)
bad, left = same_tree(a_dir, os.path.join(dw, "Swarm Game"))
check(jw["state"] == "done" and not bad and not left,
      f"finished download leaves no part/progress files, including stale ones from before ({left[:3]})")

# ---- verify: a corrupted chunk the first time ----
class CorruptOnce(Handler):
    done = [False]

    def _serve_range(self, size, read, names, gid=""):
        if names and names[0].endswith("second.pak") and "bytes=0-" in self.headers.get("Range", "") \
                and not CorruptOnce.done[0]:
            CorruptOnce.done[0] = True

            def bad(start, length):
                b = bytearray(read(start, length))
                b[100] ^= 0xFF
                return bytes(b)
            return super()._serve_range(size, bad, names, gid)
        return super()._serve_range(size, read, names, gid)


sc = serve(FakeApp("C", a_dir), CorruptOnce)
PC = {"C": {"id": "C", "name": "C", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sc.server_address[1]}}
dz = os.path.join(tmp, "dlZ")
dlz = transfer.Downloads(Cfg(download_dir=dz, folders=[]), FakeDiscovery("Z", lambda: list(PC.values())),
                         FakeLibrary(""), Sync())
dlz.start(GID, ["C"])
jz = wait(dlz)
bad, left = same_tree(a_dir, os.path.join(dz, "Swarm Game"))
check(CorruptOnce.done[0], "the source sent a corrupted chunk")
check(jz["state"] == "done" and not bad and not left,
      f"bad file detected by SHA-256, fetched again, download correct ({jz['state']} {jz['error']} {bad[:2]})")

# ---- HashCache + Library manifest ----
hc = hashcache.HashCache(tmp)
fp = os.path.join(a_dir, "data", "f1.dat")
check(hc.get(fp) == "", "unknown file has no hash")
hc.hash_folders([a_dir])
check(hc.get(fp) == sha(fp) and hc.pending == 0, "background pass hashed the game")
hc2 = hashcache.HashCache(tmp)
check(hc2.get(fp) == sha(fp), "hashes kept between runs (hash_cache.json)")
os.utime(fp, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
check(hc2.get(fp) == "", "a changed file's hash is forgotten")
from lanparty.library import Library  # noqa: E402


class LCfg(dict):
    data_dir = tmp

    def set(self, k, v):
        self[k] = v


lib = Library(LCfg(folders=[]))
lib.games = {GID: {"id": GID, "path": a_dir, "title": "Swarm Game", "folder": "Swarm Game"}}
lib.hashes.hash_folders([a_dir])
man = lib.manifest(GID)
check(all(f.get("sha256") for f in man["files"]) and
      next(f for f in man["files"] if f["p"] == "big.pak")["sha256"] == sha(os.path.join(a_dir, "big.pak")),
      "Library manifest carries every file's SHA-256")

# ---- _agree with hashes ----
srcs = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
mans = {"s1": {"files": [{"p": "a", "size": 5, "sha256": "x"}]},
        "s2": {"files": [{"p": "a", "size": 5, "sha256": "y"}]},
        "s3": {"files": [{"p": "a", "size": 5, "sha256": "x"}]}}
files, has = transfer.Downloads._agree(srcs, mans)
check(files[0]["sha256"] == "x" and has["s2"] == set() and has["s1"] == {"a"},
      "a source with the same size but another hash isn't used")
mans["s2"]["files"][0].pop("sha256")
files, has = transfer.Downloads._agree(srcs, mans)
check(has["s2"] == {"a"}, "a source that hasn't hashed the file yet still serves it (checked on arrival)")

# ---- play-time limit ----
gdir = os.path.join(tmp, "Games", "Sleepy")
os.makedirs(gdir)
exe = os.path.join(gdir, "game")
shutil.copy(shutil.which("sleep") or "/bin/sleep", exe)
proc = subprocess.Popen([exe, "30"])
try:
    pl = throttle.Playing()
    pl.games = lambda: [(gdir, "Sleepy")]
    time.sleep(0.3)
    pl.check()
    check(pl.active and pl.title == "Sleepy", f"game watcher sees a program running from a game folder ({pl.title})")
finally:
    proc.kill()
    proc.wait()
pl.check()
check(not pl.active, "and notices when it stops")

lim_dir = os.path.join(tmp, "L")
write(os.path.join(lim_dir, "x.bin"), os.urandom(12 * (1 << 20)))
sl = serve(FakeApp("L", lim_dir))
PL = {"L": {"id": "L", "name": "L", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sl.server_address[1]}}


def timed(limit_on):
    d = os.path.join(tmp, f"dlL{int(limit_on)}")
    dls = transfer.Downloads(Cfg(download_dir=d, folders=[]), FakeDiscovery("M", lambda: list(PL.values())),
                             FakeLibrary(""), Sync())
    t = time.time()
    dls.start(GID, ["L"])
    j = wait(dls)
    return time.time() - t, j


throttle._cfg = Cfg(limit_while_playing=True, limit_mbps=3)
throttle.PLAYING.force = True
throttle.PLAYING.check()
el, j = timed(True)
check(j["state"] == "done" and j["throttle"]["limited"], f"limited copy finished, card shows the limit")
check(3.0 <= el <= 9.0, f"12 MB at 3 MB/s while playing took {el:.1f}s (about 4)")
throttle.PLAYING.force = False
throttle.PLAYING.check()
el2, j = timed(False)
check(j["state"] == "done" and el2 < 2.5, f"same copy with no game running took {el2:.1f}s")
throttle._cfg = Cfg(limit_while_playing=False, limit_mbps=3)
throttle.PLAYING.force = True
throttle.PLAYING.check()
check(throttle.limit_bps() == 0, "limit off in Settings: no limit even while playing")
throttle.PLAYING.force = None

shutil.rmtree(tmp, ignore_errors=True)
print("\nALL_PASS" if not fails else f"\nFAIL ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
