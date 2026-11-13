"""Finds other launchers on the LAN with a UDP broadcast hello every 2 seconds."""
import concurrent.futures
import json
import socket
import threading
import time
import urllib.request

from . import DISCOVERY_PORT, VERSION

PEER_TIMEOUT = 10.0
VPN_SUBNET_PREFIX = "10.128.0."   # matches vpn.VPN_SUBNET


def local_ipv4s() -> list:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254."))


class Discovery:
    def __init__(self, cfg, http_port: int):
        self.cfg = cfg
        self.http_port = http_port
        self.peers = {}   # id -> {id, name, ip, port, last_seen, version}
        self._lock = threading.Lock()
        self.on_new_peer = None
        self.lib_stamp = lambda: 0.0   # set by main: when our library last changed
        self.party = lambda: ""        # set by main: our internet party id ('' outside one)
        self.downloading = lambda: []  # set by the app: game ids we're downloading (other downloaders use us too)
        self._vpn_peers: list = []     # [(ip, port)] — VPN subnet peers to poll

    def start(self, broadcast: bool = True) -> None:
        """broadcast False (--no-discovery, used by the tests): no UDP hello sent or heard, only the --peer
        addresses are polled, so test launchers never meet the real ones on the LAN."""
        if broadcast:
            threading.Thread(target=self._listen, daemon=True).start()
            threading.Thread(target=self._announce, daemon=True).start()
        threading.Thread(target=self._poll_static, daemon=True).start()

    def hello(self) -> dict:
        # "pm": this player is the Party Master (starts the polls). Older launchers ignore the key.
        return {"app": "lanparty", "v": VERSION, "id": self.cfg["peer_id"], "name": self.cfg["name"],
                "port": self.http_port, "lib": self.lib_stamp(), "ips": local_ipv4s(), "party": self.party(),
                "pm": bool(self.cfg.get("party_master", False)),
                "dl": [str(g)[:64] for g in self.downloading()][:8]}

    def is_party_master(self, pid: str) -> bool:
        """Whether a peer that is online right now says it's the Party Master (this PC: its own setting)."""
        if pid == self.cfg["peer_id"]:
            return bool(self.cfg.get("party_master", False))
        p = self.get(pid)
        return bool(p and p.get("party_master"))

    def list(self) -> list:
        now = time.time()
        with self._lock:
            return [dict(p) for p in self.peers.values() if now - p["last_seen"] < PEER_TIMEOUT]

    def get(self, pid: str):
        for p in self.list():
            if p["id"] == pid:
                return p
        return None

    def seen(self, msg: dict, ip: str) -> None:
        if msg.get("app") != "lanparty" or msg.get("id") == self.cfg["peer_id"]:
            return
        pid = str(msg["id"])
        advertised = [str(a) for a in msg.get("ips", []) if a] or []
        all_ips = list(dict.fromkeys([ip] + advertised))
        with self._lock:
            new = pid not in self.peers or time.time() - self.peers[pid]["last_seen"] >= PEER_TIMEOUT
            self.peers[pid] = {"id": pid, "name": str(msg.get("name", "?"))[:40], "ip": ip,
                               "ips": all_ips, "port": int(msg.get("port", 0)), "version": msg.get("v", ""),
                               "lib": float(msg.get("lib", 0) or 0), "party": str(msg.get("party") or "")[:32],
                               "party_master": msg.get("pm") is True,
                               "dl": [str(g)[:64] for g in (msg.get("dl") if isinstance(msg.get("dl"), list) else [])][:8],
                               "last_seen": time.time()}
        if new and self.on_new_peer:
            self.on_new_peer(pid)

    def _listen(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError as e:
            print("Discovery listen failed:", e)
            return
        while True:
            try:
                data, addr = s.recvfrom(4096)
                self.seen(json.loads(data.decode("utf-8")), addr[0])
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def vpn_up(self, ips: list = None) -> bool:
        with self._lock:
            if self._vpn_peers:
                return True
        return any(ip.startswith(VPN_SUBNET_PREFIX) for ip in (local_ipv4s() if ips is None else ips))

    def announce_plan(self, ips: list = None) -> list:
        """[(bind_ip, target)]: bind_ip None is the unbound socket. An unbound 255.255.255.255 leaves through one
        adapter only (the lowest metric), so each local address also sends from a socket bound to it."""
        ips = local_ipv4s() if ips is None else ips
        targets = {"255.255.255.255"}
        for ip in ips:
            targets.add(".".join(ip.split(".")[:3] + ["255"]))   # assumes /24, the usual home/LAN-party net
        if self.vpn_up(ips):
            targets.add(VPN_SUBNET_PREFIX + "255")
        plan = [(None, t) for t in sorted(targets)]
        for ip in ips:
            own = {"255.255.255.255", ".".join(ip.split(".")[:3] + ["255"])}
            if VPN_SUBNET_PREFIX + "255" in targets:
                own.add(VPN_SUBNET_PREFIX + "255")
            plan += [(ip, t) for t in sorted(own)]
        return plan

    def _announce(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        bound: dict = {}     # local ip -> socket bound to it
        while True:
            msg = json.dumps(self.hello()).encode("utf-8")
            ips = local_ipv4s()
            for ip in [ip for ip in bound if ip not in ips]:      # adapter went away
                bound.pop(ip).close()
            for ip, t in self.announce_plan(ips):
                sock = s
                if ip is not None:
                    sock = bound.get(ip)
                    if sock is None:
                        try:
                            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        except OSError:
                            continue
                        try:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                            sock.bind((ip, 0))
                            bound[ip] = sock
                        except OSError:
                            sock.close()
                            continue
                try:
                    sock.sendto(msg, (t, DISCOVERY_PORT))
                except OSError:
                    pass
            time.sleep(2.0)

    def add_vpn_peer(self, ip: str, port: int) -> None:
        """Add a VPN subnet address to poll for peers."""
        entry = (ip, port)
        with self._lock:
            if entry not in self._vpn_peers:
                self._vpn_peers.append(entry)

    def clear_vpn_peers(self) -> None:
        with self._lock:
            self._vpn_peers.clear()

    def _hello_at(self, host: str, port: str) -> bool:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/peer/hello", timeout=1.5) as r:
                self.seen(json.loads(r.read().decode("utf-8")), host)
                return True
        except (OSError, ValueError):
            return False

    def _poll_static(self) -> None:
        """Peers typed in by hand (ip:port) and VPN subnet peers.

        Known addresses are polled every 3 s (a peer is dropped after PEER_TIMEOUT = 10 s). The whole VPN subnet is
        swept every 15 s, in parallel: one at a time, 254 addresses at up to 1.5 s each took minutes, so peers
        expired before the sweep came back round to them."""
        found: dict = {}          # vpn address -> last time it answered
        last_sweep = 0.0
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=64)
        while True:
            targets = set()
            for entry in list(self.cfg.get("static_peers", [])):
                host, _, port = entry.partition(":")
                targets.add((host, port or "47801"))
            with self._lock:
                vpn = list(self._vpn_peers)
            if not vpn:
                found.clear()
            for ip, port in vpn:
                targets.add((ip, str(port)))
            now = time.time()
            targets.update((ip, "47801") for ip, t in found.items() if now - t < 60)
            if vpn and now - last_sweep >= 15.0:
                last_sweep = now
                targets.update((f"{VPN_SUBNET_PREFIX}{i}", "47801") for i in range(1, 255))
            futs = {pool.submit(self._hello_at, h, p): h for h, p in targets}
            for f in concurrent.futures.as_completed(futs):
                if f.result() and futs[f].startswith(VPN_SUBNET_PREFIX):
                    found[futs[f]] = time.time()
            time.sleep(3.0)
