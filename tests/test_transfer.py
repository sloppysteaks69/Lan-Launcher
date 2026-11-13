"""End-to-end test of the multi-source downloader against real launcher HTTP servers on this PC, with party signing on.
Source A: full copy. Source B: an older launcher (no /peer/bundle, ignores the end of a byte range), with one file in
a different version and one file missing. Source C: offline. Then a resume after an interruption.
Run: python tests\\test_transfer.py"""
import hashlib
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import peers, security, transfer  # noqa: E402
from lanparty.server import Handler, Server as ThreadingHTTPServer  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


GID = "testgame"
SECRET = security.make_party_secret()
PARTY = "tparty"


class FakeLibrary:
    def __init__(self, path):
        self.path = path

    def has(self, gid):
        return self.get(gid) is not None

    def get(self, gid):
        return {"path": self.path, "title": "Test Game", "folder": "Test Game"} if gid == GID else None

    def manifest(self, gid):
        if gid != GID:
            return None
        out = []
        for root, _, files in os.walk(self.path):
            for fn in files:
                full = os.path.join(root, fn)
                out.append({"p": os.path.relpath(full, self.path).replace("\\", "/"), "size": os.path.getsize(full)})
        return {"id": GID, "title": "Test Game", "folder": "Test Game", "files": out}

    def resolve_file(self, gid, rel):
        if gid != GID or not rel:
            return None
        base = os.path.realpath(self.path)
        full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if os.path.commonpath([base, full]) != base or not os.path.isfile(full):
            return None
        return full


class FakeDiscovery:
    def __init__(self, pid):
        self.pid = pid

    def hello(self):
        return {"app": "lanparty", "id": self.pid, "name": self.pid}


class FakeApp:
    def __init__(self, pid, path):
        self.cfg = {"share_library": True}
        self.library = FakeLibrary(path)
        self.discovery = FakeDiscovery(pid)
        self.auth = security.PartyAuth(PARTY, SECRET)


class OldHandler(Handler):
    """Behaves like a launcher from before this change."""

    def _serve_bundle(self, gid, body):
        return self._json({"error": "unknown"}, 404)

    def _serve_file(self, full, gid=""):
        size = os.path.getsize(full)
        start = 0
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0] or 0)
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size - start))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read())


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


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


tmp = tempfile.mkdtemp(prefix="lp_transfer_")
a_dir, b_dir, dl_dir = (os.path.join(tmp, n) for n in ("A", "B", "dl"))
rnd = random.Random(7)
write(os.path.join(a_dir, "big.pak"), os.urandom(100 * (1 << 20) + 3))         # 13 chunks, last one short
write(os.path.join(a_dir, "medium.bin"), os.urandom(900 * 1024))
write(os.path.join(a_dir, "empty.txt"), b"")
for i in range(300):
    write(os.path.join(a_dir, "data", f"d{i // 50}", f"f{i}.dat"), os.urandom(rnd.randint(1, 60 * 1024)))
shutil.copytree(a_dir, b_dir)
write(os.path.join(b_dir, "data", "d0", "f3.dat"), os.urandom(77))               # different version on B
os.remove(os.path.join(b_dir, "data", "d1", "f60.dat"))                          # missing on B

sa = serve(FakeApp("A", a_dir))
sb = serve(FakeApp("B", b_dir), OldHandler)
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()

PEERS = {
    "A": {"id": "A", "name": "A", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sa.server_address[1]},
    "B": {"id": "B", "name": "B", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sb.server_address[1]},
    "C": {"id": "C", "name": "C", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": dead_port},
}


class Cfg(dict):
    def set(self, k, v):
        self[k] = v


class Disc:
    def get(self, pid):
        return PEERS.get(pid)


class Lib:
    def rescan(self):
        pass


class Sync:
    def refresh_libraries_soon(self):
        pass


peers.set_auth(PARTY, security.derive_player_key(SECRET, "me"), "me")
cfg = Cfg(download_dir=dl_dir, folders=[])
dls = transfer.Downloads(cfg, Disc(), Lib(), Sync())


def run(owners, timeout=120):
    dls.start(GID, owners)
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = dls.list()[0]
        if j["state"] in ("done", "error"):
            return j
        time.sleep(0.2)
    return dls.list()[0]


def compare():
    bad = []
    out = os.path.join(dl_dir, "Test Game")
    for root, _, files in os.walk(a_dir):
        for fn in files:
            src = os.path.join(root, fn)
            dst = os.path.join(out, os.path.relpath(src, a_dir))
            if not os.path.isfile(dst) or sha(dst) != sha(src):
                bad.append(os.path.relpath(src, a_dir))
    leftovers = [f for _, _, fs in os.walk(out) for f in fs if f.endswith((transfer.PART, transfer.PROGRESS))]
    return bad, leftovers


t0 = time.time()
j = run(["A", "B", "C"])
el = time.time() - t0
check(j["state"] == "done", f"download finished ({j['state']} {j['error']}) in {el:.1f}s")
bad, left = compare()
check(not bad, f"every file byte-identical to source A ({len(bad)} differ: {bad[:3]})")
check(not left, f"no part/progress files left ({left[:3]})")
check(j["files_done"] == j["files"] == 303, f"file count {j['files_done']}/{j['files']}")
check(j["done"] == j["total"], f"progress bytes {j['done']} == total {j['total']}")
by = {s["name"]: s["bytes"] for s in j["source_stats"]}
check(by.get("A", 0) > 0 and by.get("B", 0) > 0, f"both live sources contributed: {by}")
check(set(j["sources"]) == {"A", "B"}, f"offline source C left out: {j['sources']}")

# resume: break the big file (all chunks done but the last) and lose some small files
out = os.path.join(dl_dir, "Test Game")
big = os.path.join(out, "big.pak")
os.replace(big, big + transfer.PART)
last = (os.path.getsize(big + transfer.PART) - 1) // transfer.CHUNK
with open(big + transfer.PROGRESS, "w") as fh:
    fh.write('{"size": %d, "chunk": %d, "done": %s}' % (os.path.getsize(big + transfer.PART), transfer.CHUNK,
                                                       list(range(last))))
with open(big + transfer.PART, "r+b") as fh:           # wreck the undone last chunk to prove it is re-fetched
    fh.seek(last * transfer.CHUNK)
    fh.write(b"\0" * 3)
for i in (0, 1, 2):
    os.remove(os.path.join(out, "data", "d0", f"f{i}.dat"))
j2 = run(["A"])
check(j2["state"] == "done", f"resume finished ({j2['state']} {j2['error']})")
bad, left = compare()
check(not bad and not left, f"resume left every file correct ({bad[:3]}, {left[:3]})")
fetched = sum(s["bytes"] for s in j2["source_stats"])
check(fetched < transfer.CHUNK, f"resume fetched only what was missing ({fetched} bytes)")

# only B online: B's own version is the agreed one, and its missing file is simply not part of the game
shutil.rmtree(out)
j3 = run(["B"])
check(j3["state"] == "done", f"download from the older launcher alone finished ({j3['error'][:90]})")
check(sha(os.path.join(out, "data", "d0", "f3.dat")) == sha(os.path.join(b_dir, "data", "d0", "f3.dat")),
      "B alone: f3 is B's version")
check(sha(os.path.join(out, "big.pak")) == sha(os.path.join(b_dir, "big.pak")),
      "B alone: big file assembled from chunks of an older launcher")

sa.shutdown()
sb.shutdown()
peers.clear_auth()
shutil.rmtree(tmp, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("\nALL_PASS")
