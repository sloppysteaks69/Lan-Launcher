"""Talks to other launchers over HTTP: pulls their libraries and votes, pushes vote changes and party launches.

When an internet party is active, every outgoing /peer/* request (except /peer/hello) is signed with HMAC-SHA256."""
import concurrent.futures
import hashlib
import json
import socket
import threading
import time
import urllib.request

from . import media, security

_best_ip_cache: dict = {}
_best_ip_lock = threading.Lock()

# module-level auth state: set by __main__ or server when a party starts
_auth_key: bytes = b""      # our signing key for the current party
_auth_party_id: str = ""    # the party id
_auth_peer_id: str = ""     # our peer id


# Peers that answered a /peer/* request with 403 -> when. A peer in an internet party counts every unsigned or
# wrongly signed request as a bad signature and blocks our IP for the session after 10 (security.py), so the
# background sync must not keep knocking: it waits REFUSED_RETRY seconds, or until our own party state changes.
_refused: dict = {}
_refused_lock = threading.Lock()
REFUSED_RETRY = 300.0


def set_auth(party_id: str, key: bytes, peer_id: str) -> None:
    global _auth_key, _auth_party_id, _auth_peer_id
    _auth_key = key
    _auth_party_id = party_id
    _auth_peer_id = peer_id
    forget_refusals()


def clear_auth() -> None:
    global _auth_key, _auth_party_id, _auth_peer_id
    _auth_key = b""
    _auth_party_id = ""
    _auth_peer_id = ""
    forget_refusals()


def current_party() -> str:
    """Our internet party id, or '' outside a party (advertised in the discovery hello)."""
    return _auth_party_id


def forget_refusals(pid: str = "") -> None:
    with _refused_lock:
        if pid:
            _refused.pop(pid, None)
        else:
            _refused.clear()


def note_refused(pid: str, err) -> None:
    """Remember a peer that refused us with 403 (HTTPError is an OSError, so callers pass what they caught)."""
    if getattr(err, "code", None) == 403:
        with _refused_lock:
            _refused[pid] = time.time()


def can_sync(p: dict) -> bool:
    """Whether background sync may send this peer a /peer/* request (other than /peer/hello).
    No when its hello says it's in an internet party we aren't in (1.1.1+ hellos carry 'party'), or it refused
    us with 403 in the last REFUSED_RETRY seconds (older peers, or a party we joined with a wrong code)."""
    party = p.get("party") or ""
    if party and party != _auth_party_id:
        return False
    with _refused_lock:
        t = _refused.get(p.get("id", ""))
    return t is None or time.time() - t >= REFUSED_RETRY


def _sign_headers(method: str, path: str, body: bytes) -> dict:
    """Build the signature headers if auth is active. The server verifies the path without its query string."""
    if not _auth_key:
        return {}
    path = path.split("?", 1)[0]
    ts = security.make_timestamp()
    nonce = security.make_nonce()
    sig = security.sign_request(_auth_key, method, path, body, ts, nonce)
    return {"X-Party-Id": _auth_party_id, "X-Timestamp": ts, "X-Nonce": nonce,
            "X-Signature": sig, "X-Peer-Id": _auth_peer_id}


def peer_url(p: dict, path: str, ip: str = "") -> str:
    return f"http://{ip or p['ip']}:{p['port']}{path}"


def _tcp_latency(ip: str, port: int) -> float:
    """TCP connect time in seconds; returns float('inf') on failure."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        t0 = time.monotonic()
        s.connect((ip, port))
        elapsed = time.monotonic() - t0
        s.close()
        return elapsed
    except OSError:
        return float("inf")


def _hello_latency(ip: str, port: int, want_id: str) -> float:
    """Round trip of /peer/hello in seconds, only if the launcher that answers is the peer we mean; else inf.
    A bare TCP connect isn't enough: two PCs with the same VirtualBox/VMware adapter address (192.168.56.1) would
    'reach' themselves, and the downloader would ask its own PC for the game."""
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(f"http://{ip}:{port}/peer/hello", timeout=1.5) as r:
            msg = json.loads(r.read().decode("utf-8"))
        if str(msg.get("id")) != want_id:
            return float("inf")
        return time.monotonic() - t0
    except (OSError, ValueError):
        return float("inf")


def probe_peer(p: dict) -> dict:
    """Probe all of a peer's IPs at once and return {ip: latency_ms} for the ones where that peer answers."""
    port = p.get("port", 47801)
    ips = list(dict.fromkeys(p.get("ips") or [p["ip"]]))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(16, len(ips)))) as ex:
        for ip, lat in zip(ips, ex.map(lambda i: _hello_latency(i, port, str(p.get("id", ""))), ips)):
            if lat < float("inf"):
                results[ip] = round(lat * 1000, 2)
    return results


VPN_PREFIX = "10.128.0."   # vpn.VPN_SUBNET
VPN_PENALTY_MS = 5.0       # n2n's encrypted tunnel carries far less than a LAN: in the same room, use the LAN


def pick_ip(results: dict) -> str:
    """The address to use from {ip: latency_ms}. One /peer/hello round trip is noisy at the millisecond level (it
    includes the HTTP handling), so a VPN address only wins when it's clearly faster than every LAN address that
    answered, which in practice means the peer isn't on our LAN at all."""
    return min(results, key=lambda ip: results[ip] + (VPN_PENALTY_MS if ip.startswith(VPN_PREFIX) else 0.0))


def best_ip(p: dict, force: bool = False) -> str:
    """Return the lowest-latency reachable IP for a peer, with caching."""
    pid = p.get("id", "")
    with _best_ip_lock:
        cached = _best_ip_cache.get(pid)
        if cached and not force and time.time() - cached["at"] < 30:
            return cached["ip"]
    results = probe_peer(p)
    if not results:
        return p["ip"]
    winner = pick_ip(results)
    with _best_ip_lock:
        _best_ip_cache[pid] = {"ip": winner, "at": time.time(), "probes": results}
    return winner


def link_report() -> dict:
    """Current best-IP cache for UI display."""
    with _best_ip_lock:
        return dict(_best_ip_cache)


def get_json(p: dict, path: str, timeout: float = 4.0, ip: str = ""):
    headers = _sign_headers("GET", path, b"")
    req = urllib.request.Request(peer_url(p, path, ip), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(p: dict, path: str, body: dict, timeout: float = 4.0, ip: str = ""):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(_sign_headers("POST", path, data))
    req = urllib.request.Request(peer_url(p, path, ip), data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


class PeerSync:
    def __init__(self, cfg, discovery, library, votes):
        self.cfg = cfg
        self.discovery = discovery
        self.library = library
        self.votes = votes
        self.libraries = {}    # peer id -> {"games": [...], "at": time}
        self._lib_at = {}
        self._push = threading.Event()
        votes.on_change = self._push.set
        discovery.on_new_peer = self._new_peer

    def _new_peer(self, pid: str) -> None:
        self._lib_at.pop(pid, None)
        forget_refusals(pid)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._pusher, daemon=True).start()

    def _loop(self) -> None:
        while True:
            for p in self.discovery.list():
                if not can_sync(p):
                    continue
                try:
                    known = self.libraries.get(p["id"], {}).get("stamp")
                    if known != p.get("lib") or time.time() - self._lib_at.get(p["id"], 0) > 30:
                        stamp = p.get("lib")
                        games = get_json(p, "/peer/library")
                        try:        # Movies / TV / Music / Books / Misc (1.13.0+; older launchers answer 404)
                            items = get_json(p, "/peer/media")
                        except (OSError, ValueError):
                            items = []
                        self.libraries[p["id"]] = {"games": games, "media": items if isinstance(items, list) else [],
                                                   "at": time.time(), "stamp": stamp}
                        self._lib_at[p["id"]] = time.time()
                    self.votes.merge(get_json(p, "/peer/votes"))
                except OSError as e:
                    note_refused(p["id"], e)
                except ValueError:
                    pass
            time.sleep(1.5)

    def refresh_libraries_soon(self) -> None:
        self._lib_at.clear()
        for lib in self.libraries.values():
            lib["stamp"] = None

    def _pusher(self) -> None:
        while True:
            self._push.wait()
            self._push.clear()
            st = self.votes.state()
            for p in self.discovery.list():
                threading.Thread(target=self._safe_post, args=(p, "/peer/votes", st), daemon=True).start()

    def broadcast(self, path: str, body: dict) -> None:
        for p in self.discovery.list():
            threading.Thread(target=self._safe_post, args=(p, path, body), daemon=True).start()

    @staticmethod
    def _safe_post(p, path, body) -> None:
        if not can_sync(p):
            return
        try:
            post_json(p, path, body)
        except OSError as e:
            note_refused(p["id"], e)
        except ValueError:
            pass

    def merged_library(self) -> list:
        """Union of every library. owners = peer ids that have the game ('self' is this PC)."""
        me = self.cfg["peer_id"]
        alive = {p["id"]: p for p in self.discovery.list()}
        out = {}
        for g in self.library.public_list():
            out[g["id"]] = {**g, "owners": [me], "have": True}
        for pid, lib in list(self.libraries.items()):
            if pid not in alive:
                continue
            for g in lib["games"]:
                gid = g.get("id")
                if not gid:
                    continue
                if gid in out:
                    if pid not in out[gid]["owners"]:
                        out[gid]["owners"].append(pid)
                else:
                    out[gid] = {**g, "owners": [pid], "have": False}
        return sorted(out.values(), key=lambda g: g.get("title", "").lower())

    def merged_media(self) -> list:
        """Union of every player's media items (media.merge), same rules as merged_library."""
        alive = {p["id"] for p in self.discovery.list()}
        return media.merge(self.library.media.public_list(), self.cfg["peer_id"],
                           {pid: lib.get("media", []) for pid, lib in list(self.libraries.items())}, alive)
