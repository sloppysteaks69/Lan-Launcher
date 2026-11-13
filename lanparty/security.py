"""Request signing (HMAC-SHA256), per-player keys, nonce tracking, rate limiting, and audit log.

Every /peer/* request in an internet party carries four headers:
  X-Party-Id     — which party
  X-Timestamp    — Unix seconds (must be within ±60 s)
  X-Nonce        — random 16-hex (unique per request, tracked 120 s)
  X-Signature    — HMAC-SHA256(key, timestamp|nonce|method|path|body_sha256)

Unsigned, expired, replayed or mis-signed requests → 403, no body."""
import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict

MAX_CLOCK_DRIFT = 60        # seconds
NONCE_TTL = 120             # seconds
RATE_LIMIT = 60             # requests per minute per IP that isn't a verified member (e.g. a kicked peer)
MEMBER_RATE_LIMIT = 6000    # requests per minute for validly signed members: library/vote sync runs ~80/min
                            # per peer and a game download is one request per file; this is a flood guard only
BAD_SIG_LIMIT = 10          # bad signatures before IP is blocked for the session
BLOCK_MINUTES = 5           # rate-limit block duration


class AuditLog:
    """Ring buffer of recent peer requests, shown in the UI."""

    def __init__(self, capacity: int = 500):
        self._entries: list = []
        self._cap = capacity
        self._lock = threading.Lock()

    def add(self, ip: str, player: str, method: str, path: str, allowed: bool, reason: str = "") -> None:
        entry = {"t": time.time(), "ip": ip, "player": player, "method": method,
                 "path": path, "ok": allowed, "reason": reason}
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._cap:
                self._entries = self._entries[-self._cap:]

    def recent(self, n: int = 50) -> list:
        with self._lock:
            return list(self._entries[-n:])


class RateLimiter:
    """Per-IP request counter + bad-signature counter."""

    def __init__(self):
        self._hits: dict = defaultdict(list)       # ip -> [timestamps]
        self._bad: dict = defaultdict(int)          # ip -> count of bad sigs
        self._blocked: dict = {}                    # ip -> unblock time
        self._lock = threading.Lock()

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            until = self._blocked.get(ip)
            if until is None:
                return False
            if time.time() < until:
                return True
            del self._blocked[ip]
            return False

    def check(self, ip: str, limit: int = RATE_LIMIT) -> str:
        """Counts one request. Returns '' if allowed, or a reason string if blocked."""
        now = time.time()
        with self._lock:
            if ip in self._blocked:
                if now < self._blocked[ip]:
                    return "rate-limited"
                del self._blocked[ip]
            # sliding window: keep only the last 60 s of hits
            self._hits[ip] = [t for t in self._hits[ip] if now - t < 60]
            if len(self._hits[ip]) >= limit:
                self._blocked[ip] = now + BLOCK_MINUTES * 60
                return "rate-limited"
            self._hits[ip].append(now)
        return ""

    def bad_sig(self, ip: str) -> bool:
        """Record a bad signature. Returns True if the IP is now blocked for the session."""
        with self._lock:
            self._bad[ip] += 1
            if self._bad[ip] >= BAD_SIG_LIMIT:
                self._blocked[ip] = time.time() + 86400   # effectively session-long
                return True
        return False

    def kick_ip(self, ip: str) -> None:
        with self._lock:
            self._blocked[ip] = time.time() + 86400


class NonceTracker:
    """Rejects replayed nonces within the TTL window."""

    def __init__(self):
        self._seen: dict = {}      # nonce -> expiry time
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str) -> bool:
        """Returns True if the nonce is fresh (not seen before)."""
        now = time.time()
        with self._lock:
            # prune expired
            if len(self._seen) > 5000:
                self._seen = {n: t for n, t in self._seen.items() if t > now}
            if nonce in self._seen and self._seen[nonce] > now:
                return False
            self._seen[nonce] = now + NONCE_TTL
            return True


def derive_player_key(party_secret: bytes, peer_id: str) -> bytes:
    """Per-player signing key, derived from the party master secret."""
    return hmac.new(party_secret, peer_id.encode("utf-8"), hashlib.sha256).digest()


def sign_request(key: bytes, method: str, path: str, body: bytes, timestamp: str, nonce: str) -> str:
    """Compute HMAC-SHA256 signature for a request."""
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"{timestamp}|{nonce}|{method}|{path}|{body_hash}"
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def make_nonce() -> str:
    return os.urandom(8).hex()


def make_timestamp() -> str:
    return str(int(time.time()))


class PartyAuth:
    """Manages authentication for one internet party."""

    def __init__(self, party_id: str, party_secret: bytes):
        self.party_id = party_id
        self.secret = party_secret
        self.active_peers: dict = {}     # peer_id -> {"key": bytes, "name": str, "joined": float}
        self.kicked: set = set()         # peer_ids that were kicked
        self.nonces = NonceTracker()
        self.limiter = RateLimiter()
        self.audit = AuditLog()
        self._lock = threading.Lock()

    def add_peer(self, peer_id: str, name: str = "") -> bytes:
        """Register a peer and return their signing key."""
        key = derive_player_key(self.secret, peer_id)
        with self._lock:
            self.active_peers[peer_id] = {"key": key, "name": name, "joined": time.time()}
        return key

    def kick_peer(self, peer_id: str) -> None:
        with self._lock:
            self.kicked.add(peer_id)
            self.active_peers.pop(peer_id, None)

    def is_kicked(self, peer_id: str) -> bool:
        return peer_id in self.kicked

    def verify_request(self, ip: str, method: str, path: str, body: bytes, headers: dict) -> tuple:
        """Verify a signed peer request.

        Returns (True, peer_id, '') on success, or (False, '', reason) on failure."""
        # blocked IPs (bad signatures, kicks, floods) are refused before anything else
        if self.limiter.is_blocked(ip):
            self.audit.add(ip, "", method, path, False, "rate-limited")
            return False, "", "rate-limited"

        party_id = headers.get("x-party-id", "")
        timestamp = headers.get("x-timestamp", "")
        nonce = headers.get("x-nonce", "")
        signature = headers.get("x-signature", "")
        peer_id = headers.get("x-peer-id", "")

        if not all([party_id, timestamp, nonce, signature, peer_id]):
            self.audit.add(ip, peer_id, method, path, False, "missing headers")
            self.limiter.bad_sig(ip)
            return False, "", "missing headers"

        if party_id != self.party_id:
            self.audit.add(ip, peer_id, method, path, False, "wrong party")
            self.limiter.bad_sig(ip)
            return False, "", "wrong party"

        # clock check
        try:
            ts = int(timestamp)
        except ValueError:
            self.audit.add(ip, peer_id, method, path, False, "bad timestamp")
            self.limiter.bad_sig(ip)
            return False, "", "bad timestamp"

        if abs(time.time() - ts) > MAX_CLOCK_DRIFT:
            self.audit.add(ip, peer_id, method, path, False, "timestamp expired")
            self.limiter.bad_sig(ip)
            return False, "", "timestamp expired"

        # replay check
        if not self.nonces.check_and_add(nonce):
            self.audit.add(ip, peer_id, method, path, False, "replayed nonce")
            self.limiter.bad_sig(ip)
            return False, "", "replayed nonce"

        # kicked check
        if self.is_kicked(peer_id):
            self.audit.add(ip, peer_id, method, path, False, "kicked")
            self.limiter.check(ip)        # strict 60/min for a kicked peer that keeps knocking
            return False, "", "kicked"

        # find the peer's key
        with self._lock:
            peer = self.active_peers.get(peer_id)

        if not peer:
            # peer not yet registered — derive their key from the party secret and check
            # (they know the secret from the party code, so this is their first request)
            key = derive_player_key(self.secret, peer_id)
        else:
            key = peer["key"]

        # verify signature
        expected = sign_request(key, method, path, body, timestamp, nonce)
        if not hmac.compare_digest(signature, expected):
            self.audit.add(ip, peer_id, method, path, False, "bad signature")
            if self.limiter.bad_sig(ip):
                self.audit.add(ip, peer_id, method, path, False, "IP blocked: too many bad sigs")
            return False, "", "bad signature"

        reason = self.limiter.check(ip, MEMBER_RATE_LIMIT)
        if reason:
            self.audit.add(ip, peer_id, method, path, False, reason)
            return False, "", reason

        # auto-register on first valid request
        if not peer:
            self.add_peer(peer_id, "")

        name = (self.active_peers.get(peer_id) or {}).get("name", peer_id)
        self.audit.add(ip, name, method, path, True)
        return True, peer_id, ""

    def learn_names(self, names: dict) -> None:
        """Fill in names for members that auto-registered without one (their first signed request carries only an
        id); the names come from discovery hellos."""
        with self._lock:
            for pid, p in self.active_peers.items():
                n = names.get(pid)
                if n and p["name"] != n:
                    p["name"] = n

    def export_state(self) -> dict:
        """Security state for an update hand-over (handoff.py), so the next launcher keeps rule 9 across the restart:
        kicked players stay out, recent nonces can't be replayed, blocked IPs stay blocked. Keys aren't stored:
        they're derived from the secret again."""
        now = time.time()
        with self._lock:
            members = {pid: {"name": p["name"], "joined": p["joined"]} for pid, p in self.active_peers.items()}
            kicked = sorted(self.kicked)
        with self.nonces._lock:
            nonces = {n: t for n, t in self.nonces._seen.items() if t > now}
        with self.limiter._lock:
            bad = dict(self.limiter._bad)
            blocked = {ip: t for ip, t in self.limiter._blocked.items() if t > now}
        return {"members": members, "kicked": kicked, "nonces": nonces, "bad": bad, "blocked": blocked}

    def import_state(self, d: dict) -> None:
        now = time.time()
        d = d if isinstance(d, dict) else {}
        for pid, m in (d.get("members") or {}).items():
            if isinstance(m, dict):
                self.add_peer(str(pid), str(m.get("name", ""))[:40])
                self.active_peers[str(pid)]["joined"] = float(m.get("joined") or now)
        with self._lock:
            self.kicked.update(str(k) for k in (d.get("kicked") or []))
            for k in self.kicked:
                self.active_peers.pop(k, None)
        with self.nonces._lock:
            self.nonces._seen.update({str(n): float(t) for n, t in (d.get("nonces") or {}).items()
                                      if float(t) > now})
        with self.limiter._lock:
            for ip, n in (d.get("bad") or {}).items():
                self.limiter._bad[str(ip)] = max(self.limiter._bad[str(ip)], int(n))
            for ip, t in (d.get("blocked") or {}).items():
                if float(t) > now:
                    self.limiter._blocked[str(ip)] = float(t)

    def members(self) -> list:
        """List of active party members for the UI."""
        with self._lock:
            return [{"id": pid, "name": p["name"], "joined": p["joined"]}
                    for pid, p in self.active_peers.items()]


def make_party_secret() -> bytes:
    """Generate a 32-byte random secret for a new party."""
    return os.urandom(32)


def make_party_id() -> str:
    """Short random party identifier."""
    return os.urandom(4).hex()
