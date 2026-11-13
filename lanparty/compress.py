"""Optional compression of peer-to-peer game transfers: zstd (preferred) or LZ4.

Both modules are bundled in lanparty\\vendor (Windows builds for Python 3.10 to 3.13), so nothing is installed.
Whatever can't load is simply not offered; with neither, transfers go uncompressed.

Negotiation, per request, so mixed versions always work:
  - the downloader sends  X-LP-Accept-Encoding: zstd, lz4  (the codecs that loaded here)
  - the source picks the first one it has too, and if the bytes shrink enough replies with
        X-LP-Encoding: zstd|lz4   and   X-LP-Raw-Length: <bytes before compression>
    and a body that is one zstd frame / one raw LZ4 block. Otherwise it replies exactly as before.
  - zstd level adapts per downloader (Adaptive below): it starts at 3 on the LAN (about 250 MB/s per core) or 9
    when the link is the bottleneck (VPN / internet, or the play-time limit on), then moves a level at a time
    toward whichever of compressing and sending is slower. Levels 19-22 compress at about 2 MB/s per core, slower
    than any link, so they're never used.
An older launcher never sends the request header (so it always gets plain bytes), a 1.9/1.10 launcher only offers
lz4 (so it gets lz4), and a reply without X-LP-Encoding is read as plain bytes."""
import io
import os
import sys

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.append(_VENDOR)       # appended: a module installed with pip wins over the bundled one

ERRORS = {}
try:
    import lz4.block as _lz4
except Exception as e:             # wrong Python version / platform for the bundled build
    _lz4 = None
    ERRORS["lz4"] = str(e)
try:
    import zstandard as _zstd
except Exception as e:
    _zstd = None
    ERRORS["zstd"] = str(e)

LZ4_ERROR = ERRORS.get("lz4", "")
CODECS = [c for c, m in (("zstd", _zstd), ("lz4", _lz4)) if m is not None]    # preference order
ENABLED = bool(CODECS)
ACCEPT_HEADER = "X-LP-Accept-Encoding"
ENCODING_HEADER = "X-LP-Encoding"
RAW_LENGTH_HEADER = "X-LP-Raw-Length"
NAME = CODECS[0] if CODECS else ""      # what this PC would pick first

ZSTD_LAN = 3
ZSTD_SLOW_LINK = 9
MAX_UNIT = 16 << 20        # compress replies up to this size (held in memory); the downloader asks for 8 MB units
MIN_UNIT = 4 << 10         # below this the saving isn't worth a header
SAMPLE = 256 << 10         # try this much first; if it barely shrinks, send the rest plain too
GOOD_RATIO = 0.90          # send compressed only if it is at most 90% of the original
VPN_PREFIX = "10.128.0."   # vpn.VPN_SUBNET

# Formats that are already compressed: never worth the CPU.
SKIP_EXT = frozenset((
    ".zip", ".rar", ".7z", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".lz4", ".cab", ".pk3", ".pk4", ".pk7",
    ".mp3", ".ogg", ".opus", ".flac", ".wma", ".m4a", ".aac",
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".mov", ".bik", ".bk2", ".usm", ".ogv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".msi", ".vpk",
    ".mka", ".m4b", ".heic", ".avif", ".epub", ".cbz", ".cbr", ".docx", ".xlsx", ".pptx", ".odt",   # media / books
))


def pick(headers) -> str:
    """The codec to answer this request with ('' = plain): the first we have that the downloader accepts."""
    v = headers.get(ACCEPT_HEADER, "") or ""
    offered = [t.strip().lower() for t in v.split(",")]
    return next((c for c in CODECS if c in offered), "")


def accepts(headers) -> bool:
    return bool(pick(headers))


def worth_trying(paths) -> bool:
    """False if every file is a format that is already compressed."""
    return any(os.path.splitext(p or "")[1].lower() not in SKIP_EXT for p in paths)


def level_for(client_ip: str = "", limited: bool = False) -> int:
    """zstd level: higher when the link, not the CPU, is what limits the copy."""
    if limited or not client_ip:
        return ZSTD_SLOW_LINK if limited else ZSTD_LAN
    if client_ip.startswith(VPN_PREFIX):
        return ZSTD_SLOW_LINK
    private = client_ip.startswith(("10.", "192.168.", "127.")) or \
        (client_ip.startswith("172.") and 16 <= int((client_ip.split(".") + ["0"])[1] or 0) <= 31)
    return ZSTD_LAN if private else ZSTD_SLOW_LINK


class Adaptive:
    """zstd level per downloader, adjusted after every reply (like zstd --adapt): the source times compressing a
    piece and sending it. Sending took much longer (the network is the limit) -> one level up, so less goes over
    the wire; compressing took longer than sending (the CPU is the limit) -> one level down. Starts at level_for()
    (3 on the LAN, 9 over the VPN/internet or while play-limited) and stays within MIN_LEVEL..MAX_LEVEL
    (MAX_LEVEL_LIMITED while the play-time limit holds the link to a few MB/s anyway)."""

    MIN_LEVEL = 1
    MAX_LEVEL = 9              # above 9 zstd slows to under 40 MB/s per core for about 1% smaller data
    MAX_LEVEL_LIMITED = 12

    def __init__(self):
        self.levels: dict = {}

    def level(self, client_ip: str, limited: bool = False) -> int:
        lv = self.levels.get(client_ip)
        if lv is None:
            lv = self.levels[client_ip] = level_for(client_ip, limited)
        return min(lv, self.MAX_LEVEL_LIMITED if limited else self.MAX_LEVEL)

    def feedback(self, client_ip: str, compress_s: float, send_s: float, limited: bool = False) -> None:
        lv = self.level(client_ip, limited)
        top = self.MAX_LEVEL_LIMITED if limited else self.MAX_LEVEL
        if send_s > 1.5 * compress_s and lv < top:
            lv += 1
        elif compress_s > send_s and lv > self.MIN_LEVEL:
            lv -= 1
        self.levels[client_ip] = lv


ADAPT = Adaptive()


def _pack(codec: str, data: bytes, level: int) -> bytes:
    if codec == "zstd":
        return _zstd.ZstdCompressor(level=level).compress(data)
    return _lz4.compress(data, store_size=False)


def compress(data: bytes, codec: str = "", level: int = ZSTD_LAN):
    """The compressed bytes, or None if it isn't worth sending compressed."""
    codec = codec or NAME
    if codec not in CODECS or len(data) < MIN_UNIT:
        return None
    try:
        if len(data) > SAMPLE * 2:      # a quick LZ4-speed probe: incompressible data costs almost nothing
            probe = _pack(codec, data[:SAMPLE], 1 if codec == "zstd" else level)
            if len(probe) > SAMPLE * GOOD_RATIO:
                return None
        out = _pack(codec, data, level)
    except Exception:
        return None
    return out if len(out) <= len(data) * GOOD_RATIO else None


def request_headers() -> dict:
    return {ACCEPT_HEADER: ", ".join(CODECS)} if CODECS else {}


class Unpacked:
    """Stands in for an HTTP response whose compressed body has been unpacked into memory."""

    def __init__(self, resp, data: bytes):
        self._resp = resp
        self._buf = io.BytesIO(data)
        self._len = len(data)
        self.status = resp.status
        self.wire_bytes = 0

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def readline(self, limit: int = -1) -> bytes:
        return self._buf.readline(limit)

    def getheader(self, name: str, default=None):
        if name.lower() == "content-length":
            return str(self._len)
        return self._resp.getheader(name, default)


def _unpack(codec: str, body: bytes, raw_len: int) -> bytes:
    if codec == "zstd":
        # read at most raw_len + 1 bytes: a frame that claims (or holds) more than the header said can't eat memory
        d = _zstd.ZstdDecompressor(max_window_size=1 << 27)
        with d.stream_reader(io.BytesIO(body), read_size=1 << 20) as r:
            out, left = [], raw_len + 1
            while left > 0:
                c = r.read(min(left, 4 << 20))
                if not c:
                    break
                out.append(c)
                left -= len(c)
        return b"".join(out)
    return _lz4.decompress(body, uncompressed_size=raw_len)   # capped by the caller: a hostile size can't eat memory


def unwrap(resp, cap: int = MAX_UNIT * 2, take=None):
    """Return resp itself for a plain reply, or an in-memory plain stand-in for a compressed one.
    Raises ValueError on a bad or oversized compressed body (the unit is then retried)."""
    enc = (resp.getheader(ENCODING_HEADER, "") or "").strip().lower()
    if not enc:
        return resp
    if enc not in CODECS:
        raise ValueError(f"source sent an encoding this PC can't read: {enc}")
    try:
        raw_len = int(resp.getheader(RAW_LENGTH_HEADER, "-1"))
        wire = int(resp.getheader("Content-Length", "-1"))
    except ValueError:
        raise ValueError("bad compression headers")
    if not (0 <= raw_len <= cap) or not (0 <= wire <= cap):
        raise ValueError("compressed reply too large")
    parts, got = [], 0
    while got < wire:
        if take is not None:
            take(min(1 << 20, wire - got))     # play-time limit counts what crosses the network
        c = resp.read(min(1 << 20, wire - got))
        if not c:
            break
        parts.append(c)
        got += len(c)
    if got != wire:
        raise ValueError(f"short read ({got} of {wire} compressed bytes)")
    try:
        data = _unpack(enc, b"".join(parts), raw_len)
    except Exception as e:
        raise ValueError(f"bad {enc} data: {e}")
    if len(data) != raw_len:
        raise ValueError(f"{enc} data has the wrong length")
    out = Unpacked(resp, data)
    out.wire_bytes = wire
    return out
