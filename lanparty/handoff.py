"""Keeps an internet party connected across a launcher update restart, and remembers the last party.

Hand-over (update restart only): the old launcher leaves n2n's edge/supernode running and the game network settings
in place, and writes party_handoff.bin. The new launcher reads it once (it's deleted on read), checks the process
ids still belong to our bundled edge/supernode (same exe path and start time, so a reused pid doesn't count), takes
them over and restores the party, including the security state (kicked players, recent nonces, blocked IPs), so
rule 9 holds across the restart. A hand-over older than HANDOFF_TTL, unreadable or not matching is refused, and the
launcher cleans up as after a crash.

Last party (last_party.bin): the code a joiner joined with, or the secret a host hosted with, so a click on "Rejoin"
or "Host again" gets back into the same party after a crash or a PC restart. Kept LAST_PARTY_TTL, cleared by
Leave party, never sent to /peer/*, never in settings.json.

Both files are encrypted with Windows DPAPI for the current user (CryptProtectData), so another account on the PC
can't read the party secret, and any change to the file makes it unreadable."""
import ctypes
import json
import os
import subprocess
import sys
import time

HANDOFF_FILE = "party_handoff.bin"
HANDOFF_TTL = 120.0
LAST_PARTY_FILE = "last_party.bin"
LAST_PARTY_TTL = 24 * 3600.0
_ENTROPY = b"lanparty-party-state-v1"
IS_WIN = sys.platform == "win32"


# ---- DPAPI ----

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool):
    if not IS_WIN:
        return None
    crypt32, k32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))   # 0x1: UI_FORBIDDEN
    if not ok:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        k32.LocalFree(out.pbData)


def protect(data: bytes):
    """Encrypted bytes, or None where DPAPI isn't available (then nothing is stored)."""
    return _dpapi(data, True)


def unprotect(blob: bytes):
    """Decrypted bytes, or None if the blob was changed, made by another user, or isn't DPAPI at all."""
    try:
        return _dpapi(bytes(blob), False)
    except (OSError, ValueError):
        return None


# ---- encrypted state files ----

def _path(data_dir: str, name: str) -> str:
    return os.path.join(data_dir, name)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _write(path: str, obj: dict) -> bool:
    blob = protect(json.dumps(dict(obj, at=time.time())).encode("utf-8"))
    if blob is None:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
        return True
    except OSError:
        _remove(tmp)
        return False


def _read(path: str, ttl: float):
    """The stored dict, or None; an expired, changed or unreadable file is deleted."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    raw = unprotect(blob)
    try:
        d = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeError):
        d = None
    if not isinstance(d, dict) or not isinstance(d.get("at"), (int, float)) or not 0 <= time.time() - d["at"] <= ttl:
        _remove(path)
        return None
    return d


def save_handoff(data_dir: str, d: dict) -> bool:
    return _write(_path(data_dir, HANDOFF_FILE), d)


def take_handoff(data_dir: str):
    """Reads the hand-over and deletes it: it's used once, by the launcher started right after the update."""
    p = _path(data_dir, HANDOFF_FILE)
    d = _read(p, HANDOFF_TTL)
    _remove(p)
    return d


def handoff_waiting(data_dir: str) -> bool:
    return os.path.isfile(_path(data_dir, HANDOFF_FILE))


def save_last_party(data_dir: str, d: dict) -> bool:
    return _write(_path(data_dir, LAST_PARTY_FILE), d)


def last_party(data_dir: str):
    return _read(_path(data_dir, LAST_PARTY_FILE), LAST_PARTY_TTL)


def clear_last_party(data_dir: str) -> None:
    _remove(_path(data_dir, LAST_PARTY_FILE))


# ---- processes ----

_SYNCHRONIZE, _TERMINATE, _SET_QUOTA, _QUERY_LIMITED = 0x00100000, 0x0001, 0x0100, 0x1000
_STILL_ACTIVE = 259


def _open(pid: int, access: int):
    if not IS_WIN or not pid:
        return None
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(access, False, int(pid))
    return h or None


def _info(h) -> tuple:
    """(exe path, start time as a FILETIME integer) of an open process handle."""
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    n = wintypes.DWORD(len(buf))
    exe = buf.value if k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(n)) else ""
    t = [wintypes.FILETIME() for _ in range(4)]
    started = 0
    if k32.GetProcessTimes(ctypes.c_void_p(h), *[ctypes.byref(x) for x in t]):
        started = (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    return exe, started


def process_identity(pid: int) -> dict:
    """{pid, exe, started} for a running process, or {} if it can't be opened."""
    h = _open(pid, _QUERY_LIMITED)
    if not h:
        return {}
    try:
        exe, started = _info(h)
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
    return {"pid": int(pid), "exe": exe, "started": started} if exe else {}


def _same_exe(a: str, b: str) -> bool:
    return bool(a) and bool(b) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class Adopted:
    """A process this launcher didn't start, with the parts of subprocess.Popen that VPN uses."""

    def __init__(self, pid: int, handle):
        self.pid = pid
        self._handle = handle       # vpn._kill_on_exit reads this, like Popen._handle

    def poll(self):
        from ctypes import wintypes
        code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(ctypes.c_void_p(self._handle), ctypes.byref(code)):
            return 1
        return None if code.value == _STILL_ACTIVE else code.value

    def wait(self, timeout=None):
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(self._handle), ms)
        r = self.poll()
        if r is None:
            raise subprocess.TimeoutExpired("adopted", timeout)
        return r

    def terminate(self):
        ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(self._handle), 1)

    kill = terminate


def adopt(ident: dict, exe: str):
    """Take over a process from a hand-over if it's still the same one (pid, exe path and start time all match)."""
    if not isinstance(ident, dict) or not ident.get("pid"):
        return None
    h = _open(int(ident["pid"]), _SYNCHRONIZE | _TERMINATE | _SET_QUOTA | _QUERY_LIMITED)
    if not h:
        return None
    path, started = _info(h)
    proc = Adopted(int(ident["pid"]), h)
    if not _same_exe(path, exe) or path.lower() != str(ident.get("exe", "")).lower() \
            or started != ident.get("started") or proc.poll() is not None:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
        return None
    return proc


def stop_if_ours(ident: dict, exe: str) -> bool:
    """A hand-over that couldn't be used: stop its edge/supernode, but only if they really are ours."""
    p = adopt(ident, exe)
    if not p:
        return False
    p.terminate()
    return True
