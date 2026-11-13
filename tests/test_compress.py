"""zstd / LZ4 compression of game transfers (lanparty\\compress.py), against real launcher HTTP servers on this PC, with
party signing on. Source A: this version (compresses). Source B: behaves like 1.8.0 (never compresses). Checks the
negotiation both ways, that incompressible and already-compressed files go plain, whole downloads from A, B and both,
an older downloader against a new source, and that a bad compressed reply is refused.
Run: python tests\\test_compress.py"""
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import compress, peers, security, transfer  # noqa: E402
from lanparty.server import Handler, Server as ThreadingHTTPServer  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


check(compress.CODECS == ["zstd", "lz4"], f"zstd and lz4 load on this PC ({compress.CODECS} {compress.ERRORS})")
if compress.CODECS != ["zstd", "lz4"]:
    print("\nFAIL (nothing else can be tested without both)")
    sys.exit(1)

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
        out = []
        for root, _, files in os.walk(self.path):
            for fn in files:
                full = os.path.join(root, fn)
                out.append({"p": os.path.relpath(full, self.path).replace("\\", "/"), "size": os.path.getsize(full)})
        return {"id": GID, "title": "Test Game", "folder": "Test Game", "files": out}

    def resolve_file(self, gid, rel):
        base = os.path.realpath(self.path)
        full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if gid != GID or os.path.commonpath([base, full]) != base or not os.path.isfile(full):
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


class Handler18(Handler):
    """A 1.8.0 source: it doesn't know the header, so it never compresses."""

    def _serve_file(self, full, gid=""):
        del self.headers[compress.ACCEPT_HEADER]
        return super()._serve_file(full, gid)

    def _serve_bundle(self, gid, body):
        del self.headers[compress.ACCEPT_HEADER]
        return super()._serve_bundle(gid, body)


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


def game_like(rnd, n):
    """Compressible bytes, like game data: repeated records with some noise."""
    words = [bytes(rnd.randrange(256) for _ in range(rnd.randint(4, 24))) for _ in range(64)]
    out, size = [], 0
    while size < n:
        w = rnd.choice(words) + bytes(rnd.randrange(4))
        out.append(w)
        size += len(w)
    return b"".join(out)[:n]


tmp = tempfile.mkdtemp(prefix="lp_compress_")
a_dir, b_dir, dl_dir = (os.path.join(tmp, n) for n in ("A", "B", "dl"))
rnd = random.Random(11)
write(os.path.join(a_dir, "maps.pak"), game_like(rnd, 40 * (1 << 20) + 5))    # 5 chunks, compressible
write(os.path.join(a_dir, "music.bin"), os.urandom(20 * (1 << 20)))            # incompressible
write(os.path.join(a_dir, "intro.zip"), game_like(rnd, 9 * (1 << 20)))         # compressible but a skipped type
for i in range(200):
    write(os.path.join(a_dir, "data", f"f{i}.dat"), game_like(rnd, rnd.randint(1, 60 * 1024)))
for i in range(40):
    write(os.path.join(a_dir, "noise", f"n{i}.raw"), os.urandom(rnd.randint(1, 60 * 1024)))
shutil.copytree(a_dir, b_dir)

sa = serve(FakeApp("A", a_dir))
sb = serve(FakeApp("B", b_dir), Handler18)
peers.set_auth(PARTY, security.derive_player_key(SECRET, "me"), "me")

# ---- single requests ----
def get(port, path, headers=None, method="GET", body=b""):
    c = transfer._Conn("127.0.0.1", port)
    try:
        r = c.request(method, path, body=body, headers=headers)
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def fpath(rel):
    return f"/peer/file/{GID}?p={rel}"


ACC = {compress.ACCEPT_HEADER: "lz4"}
RANGE = {"Range": f"bytes={transfer.CHUNK}-{2 * transfer.CHUNK - 1}"}
raw = open(os.path.join(a_dir, "maps.pak"), "rb").read()[transfer.CHUNK:2 * transfer.CHUNK]

st, h, body = get(sa.server_address[1], fpath("maps.pak"), {**RANGE, **ACC})
check(st == 206 and h.get("x-lp-encoding") == "lz4" and h.get("x-lp-raw-length") == str(len(raw)),
      f"new source compresses a chunk when asked ({st}, {h.get('x-lp-encoding')})")
check(int(h.get("content-length", 0)) == len(body) < len(raw) * 0.9,
      f"compressed chunk is smaller ({len(body)} vs {len(raw)} bytes, {100 * len(body) // len(raw)}%)")
check(h.get("content-range", "").startswith(f"bytes {transfer.CHUNK}-"), "compressed chunk keeps Content-Range")
check(compress._lz4.decompress(body, uncompressed_size=len(raw)) == raw, "compressed chunk unpacks to the same bytes")

st, h, body = get(sa.server_address[1], fpath("maps.pak"), RANGE)
check(st == 206 and "x-lp-encoding" not in h and body == raw, "older downloader (no header) gets plain bytes")
st, h, body = get(sb.server_address[1], fpath("maps.pak"), {**RANGE, **ACC})
check(st == 206 and "x-lp-encoding" not in h and body == raw, "1.8.0 source answers plain even when asked")
st, h, body = get(sa.server_address[1], fpath("music.bin"), {"Range": "bytes=0-8388607", **ACC})
check("x-lp-encoding" not in h and len(body) == 8 << 20, "incompressible chunk goes plain")

calls = []
real = compress.compress
compress.compress = lambda d, *a: calls.append(len(d)) or real(d, *a)
st, h, body = get(sa.server_address[1], fpath("intro.zip"), {"Range": "bytes=0-8388607", **ACC})
compress.compress = real
check("x-lp-encoding" not in h and not calls and len(body) == 8 << 20, "a .zip isn't even tried")

names = [f"data/f{i}.dat" for i in range(50)]
bb = json.dumps({"files": names}).encode()
st, h, body = get(sa.server_address[1], f"/peer/bundle/{GID}", ACC, "POST", bb)
plain = b"".join(open(os.path.join(a_dir, n), "rb").read() for n in names)
check(h.get("x-lp-encoding") == "lz4" and len(body) < len(plain), f"bundle compressed ({len(body)} vs {len(plain)})")
data = compress._lz4.decompress(body, uncompressed_size=int(h["x-lp-raw-length"]))
head, _, rest = data.partition(b"\n")
check(rest == plain and len(json.loads(head)["sizes"]) == 50, "compressed bundle unpacks to header + files")

# ---- unwrap refuses bad replies ----
class R:
    status = 206

    def __init__(self, b, hdr):
        self.b, self.h = io.BytesIO(b), hdr

    def read(self, n=-1):
        return self.b.read(n)

    def getheader(self, k, d=None):
        return self.h.get(k, d)


# ---- zstd (this version's first choice) ----
ACCZ = {compress.ACCEPT_HEADER: "zstd, lz4"}
check(compress.request_headers() == ACCZ, f"this PC offers zstd first, then lz4 ({compress.request_headers()})")
st, h, body = get(sa.server_address[1], fpath("maps.pak"), {**RANGE, **ACCZ})
lz4_len = len(compress.compress(raw, "lz4"))
check(h.get("x-lp-encoding") == "zstd" and len(body) < lz4_len,
      f"new downloader gets zstd, smaller than lz4 ({len(body)} vs {lz4_len} bytes, {100 * len(body) // len(raw)}%)")
check(compress._unpack("zstd", body, len(raw)) == raw, "zstd chunk unpacks to the same bytes")
st, h, body = get(sb.server_address[1], fpath("maps.pak"), {**RANGE, **ACCZ})
check("x-lp-encoding" not in h and body == raw, "1.8.0 source still answers plain")
st, h, body = get(sa.server_address[1], f"/peer/bundle/{GID}", ACCZ, "POST", bb)
check(h.get("x-lp-encoding") == "zstd", "bundle goes zstd too")
check(compress.level_for("192.168.1.20") == 3 and compress.level_for("10.128.0.50") == 9 and
      compress.level_for("73.12.4.1") == 9 and compress.level_for("192.168.1.20", limited=True) == 9,
      "zstd level 3 on the LAN, 9 over the VPN/internet or while the play-time limit is on")
t = time.time()
for _ in range(3):
    compress.compress(raw, "zstd", 3)
mbps = 3 * len(raw) / (time.time() - t) / 1e6
check(mbps > 100, f"zstd level 3 keeps up with gigabit on one core ({mbps:.0f} MB/s)")
bomb = compress._zstd.ZstdCompressor(level=3).compress(b"\0" * (64 << 20))      # 64 MB of zeros, a few KB packed
try:
    compress.unwrap(R(bomb, {"X-LP-Encoding": "zstd", "X-LP-Raw-Length": str(1 << 20), "Content-Length": str(len(bomb))}))
    check(False, "a zstd frame bigger than its header says is refused")
except ValueError:
    check(True, "a zstd frame bigger than its header says is refused (no 64 MB unpacked)")


good = compress.compress(raw, "lz4")
for what, b, hdr in (
        ("wrong raw length", good, {"X-LP-Raw-Length": "100", "Content-Length": str(len(good))}),
        ("huge raw length", good, {"X-LP-Raw-Length": str(1 << 40), "Content-Length": str(len(good))}),
        ("garbage body", os.urandom(5000), {"X-LP-Raw-Length": "9000", "Content-Length": "5000"}),
        ("short body", good[:100], {"X-LP-Raw-Length": str(len(raw)), "Content-Length": str(len(good))}),
        ("unknown encoding", good, {"X-LP-Encoding": "brotli", "X-LP-Raw-Length": "1", "Content-Length": "1"})):
    hdr.setdefault("X-LP-Encoding", "lz4")
    try:
        compress.unwrap(R(b, hdr))
        check(False, f"bad reply refused: {what}")
    except ValueError:
        check(True, f"bad reply refused: {what}")
check(compress.unwrap(R(good, {"X-LP-Encoding": "lz4", "X-LP-Raw-Length": str(len(raw)),
                               "Content-Length": str(len(good))})).read() == raw, "good reply unpacks")

# ---- adaptive zstd level ----
ad = compress.Adaptive()
check(ad.level("192.168.1.20") == 3 and ad.level("10.128.0.50") == 9, "adaptive level starts at 3 (LAN) / 9 (VPN)")
for _ in range(10):
    ad.feedback("192.168.1.20", 0.02, 0.2)        # network much slower than compressing
check(ad.level("192.168.1.20") == ad.MAX_LEVEL, f"slow network: level climbs to {ad.MAX_LEVEL}")
for _ in range(20):
    ad.feedback("192.168.1.20", 0.05, 0.01)       # CPU slower than the network
check(ad.level("192.168.1.20") == ad.MIN_LEVEL, f"slow CPU: level drops to {ad.MIN_LEVEL}")
ad.feedback("192.168.1.20", 0.05, 0.06)
check(ad.level("192.168.1.20") == ad.MIN_LEVEL, "balanced: level stays")
for _ in range(20):
    ad.feedback("10.128.0.50", 0.02, 1.0, limited=True)
check(ad.level("10.128.0.50", True) == ad.MAX_LEVEL_LIMITED and ad.level("10.128.0.50") == ad.MAX_LEVEL,
      "play-time limit lets it go to 12; back to at most 9 when the limit is off")
compress.ADAPT.levels.clear()
get(sa.server_address[1], fpath("maps.pak"), {**RANGE, **ACCZ})
check(compress.ADAPT.levels.get("127.0.0.1") in (2, 3, 4), f"a real reply fed the adapter (level {compress.ADAPT.levels})")

# ---- whole downloads ----
PEERS = {
    "A": {"id": "A", "name": "A", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sa.server_address[1]},
    "B": {"id": "B", "name": "B", "ip": "127.0.0.1", "ips": ["127.0.0.1"], "port": sb.server_address[1]},
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


def download(owners, label):
    shutil.rmtree(dl_dir, ignore_errors=True)
    dls = transfer.Downloads(Cfg(download_dir=dl_dir, folders=[]), Disc(), Lib(), Sync())
    t0 = time.time()
    dls.start(GID, owners)
    while time.time() - t0 < 120:
        j = dls.list()[0]
        if j["state"] in ("done", "error"):
            break
        time.sleep(0.2)
    out = os.path.join(dl_dir, "Test Game")
    bad = []
    for root, _, files in os.walk(a_dir):
        for fn in files:
            src = os.path.join(root, fn)
            dst = os.path.join(out, os.path.relpath(src, a_dir))
            if not os.path.isfile(dst) or sha(dst) != sha(src):
                bad.append(os.path.relpath(src, a_dir))
    check(j["state"] == "done" and not bad,
          f"{label}: done in {time.time() - t0:.1f}s, every file identical ({j['state']} {j['error']} {bad[:3]})")
    check(j["done"] == j["total"], f"{label}: progress counts original bytes ({j['done']} of {j['total']})")


download(["A"], "from the new source (zstd)")
compress.CODECS = ["lz4"]        # both ends as 1.9/1.10 launchers: lz4 only
download(["A", "B"], "lz4 only (1.10 launchers)")
compress.CODECS = ["zstd", "lz4"]
download(["B"], "from a 1.8.0 source")
download(["A", "B"], "from both")
compress.ENABLED = False         # this PC as an older downloader: sends no header, new source A answers plain
download(["A"], "older downloader from the new source")
compress.ENABLED = True

shutil.rmtree(tmp, ignore_errors=True)
print("\nALL_PASS" if not fails else f"\nFAIL ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
