"""Bandwidth limit while a game is running on this PC.

PLAYING watches the running programs every few seconds: a program inside one of this PC's game folders (or the
DOSBox the launcher uses) counts as playing. While it is, game copies to and from this PC (UP: serving peers, DOWN:
our own downloads) are held to the limit in Settings ("limit_while_playing", "limit_mbps", MB/s each way), so a big
copy doesn't lag the match. Background hashing (hashcache.py) pauses too. Nothing changes when no game runs."""
import os
import sys
import threading
import time

CHECK_EVERY = 3.0
DEFAULT_LIMIT_MBPS = 1.0
SLICE = 64 << 10


def _windows_exes() -> list:
    import ctypes
    from ctypes import wintypes
    psapi = ctypes.WinDLL("psapi")
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    arr = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    out = []
    buf = ctypes.create_unicode_buffer(1024)
    for pid in arr[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                out.append(buf.value)
        finally:
            k32.CloseHandle(h)
    return out


def _proc_exes() -> list:
    out = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            out.append(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            pass
    return out


def running_exes() -> list:
    try:
        return _windows_exes() if sys.platform == "win32" else _proc_exes()
    except Exception:
        return []


class Playing:
    """Whether a game is running here right now (checked in the background)."""

    def __init__(self):
        self.active = False
        self.title = ""
        self.force = None              # tests: True / False overrides the check
        self.games = lambda: []        # set by configure(): [(folder, title)]
        self.extra = lambda: []        # [(exe path, title)]: the DOSBox the launcher uses
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def check(self) -> None:
        if self.force is not None:
            self.active, self.title = bool(self.force), "test game" if self.force else ""
            return
        folders = [(os.path.normcase(os.path.normpath(p)) + os.sep, t) for p, t in self.games() if p]
        extra = {os.path.normcase(os.path.normpath(p)): t for p, t in self.extra() if p}
        title = ""
        for exe in running_exes():
            n = os.path.normcase(os.path.normpath(exe))
            if n in extra:
                title = extra[n]
                break
            hit = next((t for f, t in folders if n.startswith(f)), None)
            if hit:
                title = hit
                break
        self.active, self.title = bool(title), title

    def _loop(self) -> None:
        while True:
            try:
                self.check()
            except Exception:
                pass
            time.sleep(CHECK_EVERY)


PLAYING = Playing()


class Bucket:
    """Token bucket; take(n) waits until n bytes may pass. rate() 0 = no limit (take returns at once)."""

    def __init__(self, rate=lambda: 0):
        self.rate = rate
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._t = time.monotonic()

    def take(self, n: int) -> None:
        while n > 0:
            rate = self.rate()
            if not rate:
                return
            part = min(n, SLICE)
            with self._lock:
                now = time.monotonic()
                self._tokens = min(rate * 0.5, self._tokens + (now - self._t) * rate)   # at most 0.5 s burst
                self._t = now
                if self._tokens >= part:
                    self._tokens -= part
                    n -= part
                    continue
                wait = (part - self._tokens) / rate
            time.sleep(min(max(wait, 0.005), 0.25))


_cfg = None


def limit_bps() -> int:
    """Bytes per second each way right now (0 = unlimited)."""
    if not PLAYING.active or _cfg is None or not _cfg.get("limit_while_playing", True):
        return 0
    try:
        mbps = float(_cfg.get("limit_mbps", DEFAULT_LIMIT_MBPS))
    except (TypeError, ValueError):
        mbps = DEFAULT_LIMIT_MBPS
    return int(max(0.05, mbps) * 1e6)


UP = Bucket(limit_bps)
DOWN = Bucket(limit_bps)


def configure(cfg, library, net=None) -> None:
    """Called once at start: what counts as a game, where the settings are; starts the watcher."""
    global _cfg
    _cfg = cfg
    PLAYING.games = lambda: [(g.get("path"), g.get("title", "")) for g in list(library.games.values())]
    if net is not None:
        def extra():
            try:
                p = net.dosbox_path()
            except Exception:
                p = ""
            return [(p, "a DOS game")] if p else []
        PLAYING.extra = extra
    PLAYING.start()


def status() -> dict:
    bps = limit_bps()
    return {"playing": PLAYING.active, "title": PLAYING.title, "limited": bool(bps), "limit_bps": bps}


def write(wfile, data: bytes) -> None:
    """wfile.write(data), held to the upload limit while a game runs."""
    if not UP.rate():
        wfile.write(data)
        return
    mv = memoryview(data)
    for i in range(0, len(mv), SLICE):
        piece = mv[i:i + SLICE]
        UP.take(len(piece))
        wfile.write(piece)
