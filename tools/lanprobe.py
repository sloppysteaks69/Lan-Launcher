"""lanprobe: check whether two PCs can find each other the way old LAN games do.

Run one copy on each PC. Every copy sends the kinds of "is anyone there?" packets games send, answers the
ones it hears, and reports which kinds got through, on which adapter, and in which direction.

    python lanprobe.py                      plain mode: prints a summary every few seconds (Ctrl+C stops)
    python lanprobe.py --port 26214         use a game's own port (close the game first)
    python lanprobe.py --peer 10.128.0.2    also send straight to that address (proves the tunnel itself works)
    python lanprobe.py --seconds 20 --json  run 20 s, print one JSON report, exit
    python lanprobe.py --diagnose           adapters, metrics, network profiles, firewall; no packets
    python lanprobe.py --mcp                MCP server on stdin/stdout (tools: lanprobe_*)

What it sends, every 2 s, to UDP <port> (default 47810):
  limited-any       255.255.255.255 from a socket bound to 0.0.0.0. This is what most games do; Windows picks ONE
                    adapter for it (the one with the best route), which is the usual reason VPN players can't
                    see each other.
  limited-via-<ip>  255.255.255.255 from a socket bound to that adapter's address (out of that adapter).
  subnet-<bcast>    the adapter's subnet broadcast, e.g. 10.128.0.255.
  multicast         239.255.47.10 on each adapter.
  unicast-<ip>      straight to each --peer address.
Answers go back by unicast, so each side also learns which of its packets the other side heard.
It also tries TCP to the other side's sender address and to its "first IP" (the address a game gets when it asks
Windows for "my IP"; games that advertise that address, like Dark Reign 2's auto-detected local address, give
out an unreachable address when that is the home network instead of the VPN).

Python 3.10+, standard library only.
"""
import argparse
import ipaddress
import json
import os
import selectors
import socket
import subprocess
import sys
import threading
import time
import uuid

VERSION = "1.0"
MAGIC = "LANPROBE1"
DEFAULT_PORT = 47810
MCAST_GROUP = "239.255.47.10"
VPN_NET = ipaddress.ip_network("10.128.0.0/24")
VPN_ALIAS = "LanPartyVPN"
IS_WIN = os.name == "nt"
PROBE_EVERY = 2.0
TCP_EVERY = 10.0
RESCAN_EVERY = 15.0
PEER_EXPIRE = 30.0


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- system facts

def _ps(cmd, timeout=20):
    """Run a PowerShell command, return parsed JSON (always a list) or []."""
    if not IS_WIN:
        return []
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout.strip()
        if not out:
            return []
        v = json.loads(out)
        return v if isinstance(v, list) else [v]
    except Exception as e:  # noqa: BLE001
        log("powershell failed:", e)
        return []


def interfaces():
    """IPv4 addresses: [{ip, prefix, alias, metric, broadcast, vpn, loopback}]."""
    rows = []
    if IS_WIN:
        cmd = ("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { "
               "$i = Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
               "[pscustomobject]@{ip=$_.IPAddress; prefix=[int]$_.PrefixLength; alias=$_.InterfaceAlias; "
               "metric=$i.InterfaceMetric; state=[string]$i.ConnectionState} } | ConvertTo-Json -Compress")
        for r in _ps(cmd):
            if r.get("state") and r["state"] not in ("Connected", "1"):
                continue
            rows.append({"ip": r["ip"], "prefix": r.get("prefix") or 24, "alias": r.get("alias", ""),
                         "metric": r.get("metric")})
    else:
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True,
                                 timeout=5).stdout
            for line in out.splitlines():
                p = line.split()
                if "inet" in p:
                    ip, prefix = p[p.index("inet") + 1].split("/")
                    rows.append({"ip": ip, "prefix": int(prefix), "alias": p[1], "metric": None})
        except Exception:  # noqa: BLE001
            pass
    if not rows:  # fallback: host name lookup plus the routed addresses, guess /24
        for ip in first_ips() + [route_source("1.1.1.1"), route_source("10.128.0.1")]:
            if ip and ip not in [r["ip"] for r in rows]:
                rows.append({"ip": ip, "prefix": 24, "alias": "?", "metric": None})
    for r in rows:
        net = ipaddress.ip_interface(f"{r['ip']}/{r['prefix']}").network
        r["broadcast"] = str(net.broadcast_address) if net.prefixlen < 31 else None
        addr = ipaddress.ip_address(r["ip"])
        r["loopback"] = addr.is_loopback
        r["apipa"] = addr.is_link_local
        r["vpn"] = addr in VPN_NET or VPN_ALIAS.lower() in r["alias"].lower()
    return rows


def first_ips():
    """What a game gets when it asks for this PC's address (host name lookup), in order."""
    seen = []
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if ip not in seen:
                seen.append(ip)
    except OSError:
        pass
    return seen


def broadcast_source():
    """The adapter address a plain 255.255.255.255 broadcast leaves from (lowest route+interface metric)."""
    if IS_WIN:
        rows = _ps("Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 255.255.255.255/32 -ErrorAction "
                   "SilentlyContinue | ForEach-Object { $i = Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex "
                   "-AddressFamily IPv4; $a = (Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 "
                   "-ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress; [pscustomobject]@{ip=$a; "
                   "alias=$i.InterfaceAlias; m=[int]$_.RouteMetric + [int]$i.InterfaceMetric; "
                   "state=[string]$i.ConnectionState} } | ConvertTo-Json -Compress")
        rows = [r for r in rows if r.get("ip") and not str(r["ip"]).startswith("127.")
                and r.get("state") in ("Connected", "1")]
        if rows:
            return min(rows, key=lambda r: r.get("m") or 9999)["ip"]
    return route_source("255.255.255.255")


def route_source(dest):
    """The local address Windows/Linux would send to `dest` from (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def diagnose():
    ifs = interfaces()
    d = {
        "host": socket.gethostname(),
        "interfaces": ifs,
        "first_ips": first_ips(),
        "broadcast_goes_out_from": broadcast_source(),
        "internet_goes_out_from": route_source("1.1.1.1"),
    }
    if IS_WIN:
        d["network_profiles"] = _ps("Get-NetConnectionProfile | Select-Object InterfaceAlias,"
                                    "@{n='category';e={[string]$_.NetworkCategory}} | ConvertTo-Json -Compress")
        d["firewall"] = _ps("Get-NetFirewallProfile | Select-Object Name,@{n='enabled';e={[string]$_.Enabled}},"
                            "@{n='inbound';e={[string]$_.DefaultInboundAction}} | ConvertTo-Json -Compress")
    d["notes"] = diagnose_notes(d)
    return d


def diagnose_notes(d):
    notes = []
    vpn = [i for i in d["interfaces"] if i["vpn"]]
    if not vpn:
        notes.append("No VPN adapter (LanPartyVPN / 10.128.0.x) is up on this PC.")
        return notes
    vip = vpn[0]["ip"]
    b = d["broadcast_goes_out_from"]
    if b and b != vip:
        owner = next((i["alias"] for i in d["interfaces"] if i["ip"] == b), b)
        notes.append(f"Plain broadcasts (255.255.255.255) leave through '{owner}' ({b}), not the VPN ({vip}). "
                     "Games that broadcast this way won't be seen over the VPN. Fix: "
                     f"Set-NetIPInterface -InterfaceAlias {vpn[0]['alias']} -InterfaceMetric 1")
    fi = d["first_ips"]
    if fi and fi[0] != vip:
        notes.append(f"This PC's 'first IP' is {fi[0]}, not the VPN address {vip}. Games that auto-detect their "
                     "address (Dark Reign 2's local address, many DirectPlay games) will advertise the wrong one.")
    for p in d.get("network_profiles", []):
        if VPN_ALIAS.lower() in str(p.get("InterfaceAlias", "")).lower() and p.get("category") != "Private":
            notes.append(f"The VPN network is '{p.get('category')}', not Private; game firewall rules usually "
                         "only apply on Private.")
    return notes


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self, port=DEFAULT_PORT, target_port=None, peers=(), name=None, include_loopback=False):
        self.id = uuid.uuid4().hex[:8]
        self.name = name or socket.gethostname()
        self.port = port
        self.target_port = target_port or port
        self.peers = list(peers)
        self.include_loopback = include_loopback
        self.lock = threading.RLock()
        self.seen = {}       # peer id -> record
        self.bound = []      # [(label, local_ip or "0.0.0.0")]
        self.bind_errors = []
        self.socks = []      # [(sock, bind_ip)]
        self.tcp = None
        self.ifs = []
        self.seq = 0
        self.sent = {}       # method -> count
        self.started = time.time()
        self.stop_ev = threading.Event()
        self.sel = None
        self.threads = []

    # -- sockets
    def _bind(self):
        self._close_socks()
        self.ifs = [i for i in interfaces() if self.include_loopback or not i["loopback"]]
        self.sel = selectors.DefaultSelector()
        self.bound, self.bind_errors, self.socks = [], [], []
        group = socket.inet_aton(MCAST_GROUP)
        if IS_WIN:  # Windows: a socket bound to an adapter address receives that adapter's broadcasts
            for i in self.ifs:
                self._udp(i["ip"], group)
        self._udp("0.0.0.0", group)  # Linux needs the wildcard socket for broadcasts; on Windows it may fail
        try:
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if not IS_WIN:  # Linux: allow quick restarts (TIME_WAIT); on Windows this flag would allow port theft
                t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            t.bind(("0.0.0.0", self.port))
            t.listen(8)
            t.setblocking(False)
            self.tcp = t
            self.sel.register(t, selectors.EVENT_READ, ("tcp", None))
            self.bound.append(("tcp", "0.0.0.0"))
        except OSError as e:
            self.bind_errors.append(f"TCP 0.0.0.0:{self.port}: {e}")

    def _udp(self, ip, group):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, self.port))
        except OSError as e:
            s.close()
            if not (IS_WIN and ip == "0.0.0.0" and self.socks):
                self.bind_errors.append(f"UDP {ip}:{self.port}: {e}")
            return
        joins = [ip] if ip != "0.0.0.0" else [i["ip"] for i in self.ifs]
        if ip == "0.0.0.0" and IS_WIN and self.socks:
            joins = []
        for j in joins:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton(j))
            except OSError:
                pass
        s.setblocking(False)
        self.socks.append((s, ip))
        self.sel.register(s, selectors.EVENT_READ, ("udp", ip))
        self.bound.append(("udp", ip))

    def _close_socks(self):
        for s, _ in self.socks:
            try:
                s.close()
            except OSError:
                pass
        if self.tcp:
            self.tcp.close()
            self.tcp = None
        if self.sel:
            self.sel.close()
        self.socks = []

    def _sock_for(self, ip):
        for s, b in self.socks:
            if b == ip:
                return s
        return None

    # -- lifecycle
    def start(self):
        with self.lock:
            self._bind()
        for fn in (self._recv_loop, self._send_loop, self._tcp_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self):
        self.stop_ev.set()
        time.sleep(0.3)
        with self.lock:
            self._close_socks()

    # -- packets
    def _packet(self, kind, method, extra=None):
        with self.lock:
            sees = {pid: sorted(r["heard"].keys()) for pid, r in self.seen.items()}
        p = {"m": MAGIC, "t": kind, "id": self.id, "name": self.name, "method": method, "ts": time.time(),
             "first_ip": (first_ips() or [None])[0], "sees": sees}
        if extra:
            p.update(extra)
        return json.dumps(p).encode()

    def _send(self, sock, dest, method):
        try:
            sock.sendto(self._packet("probe", method), (dest, self.target_port))
            self.sent[method] = self.sent.get(method, 0) + 1
        except OSError as e:
            self.sent[method + " (error)"] = str(e)

    def probe_once(self):
        with self.lock:
            wild = self._sock_for("0.0.0.0")
            tmp = None
            if wild is None:  # Windows with per-adapter sockets: send limited-any like an unbound game socket
                tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                tmp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._send(wild or tmp, "255.255.255.255", "limited-any")
            for i in self.ifs:
                s = self._sock_for(i["ip"]) or wild or tmp
                if self._sock_for(i["ip"]):
                    self._send(s, "255.255.255.255", f"limited-via-{i['ip']}")
                if i["broadcast"]:
                    self._send(s, i["broadcast"], f"subnet-{i['broadcast']}")
                try:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(i["ip"]))
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                    self._send(s, MCAST_GROUP, f"multicast-via-{i['ip']}")
                except OSError:
                    pass
            for p in self.peers:
                src = self._sock_for(route_source(p) or "") or wild or tmp
                self._send(src, p, f"unicast-{p}")
            if tmp:
                tmp.close()

    def _send_loop(self):
        last_scan = time.time()
        while not self.stop_ev.is_set():
            try:
                self.probe_once()
            except Exception as e:  # noqa: BLE001
                log("probe error:", e)
            if time.time() - last_scan > RESCAN_EVERY:
                last_scan = time.time()
                new = sorted(i["ip"] for i in interfaces() if self.include_loopback or not i["loopback"])
                if new != sorted(i["ip"] for i in self.ifs):
                    log("adapters changed, rebinding:", new)
                    with self.lock:
                        self._bind()
            self.stop_ev.wait(PROBE_EVERY)

    def _iface_for(self, src_ip, bind_ip):
        if bind_ip and bind_ip != "0.0.0.0":
            return bind_ip
        a = ipaddress.ip_address(src_ip)
        for i in self.ifs:
            if a in ipaddress.ip_interface(f"{i['ip']}/{i['prefix']}").network:
                return i["ip"]
        return "?"

    def _recv_loop(self):
        while not self.stop_ev.is_set():
            with self.lock:
                sel = self.sel
            try:
                events = sel.select(timeout=0.5)
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            for key, _ in events:
                kind, bind_ip = key.data
                try:
                    if kind == "tcp":
                        c, _a = key.fileobj.accept()
                        c.settimeout(2)
                        c.sendall(json.dumps({"m": MAGIC, "id": self.id, "name": self.name}).encode() + b"\n")
                        c.close()
                        continue
                    data, addr = key.fileobj.recvfrom(65535)
                except (ConnectionResetError, BlockingIOError, OSError):
                    continue  # Windows reports ICMP port-unreachable on UDP sockets as resets
                self._handle(data, addr, bind_ip, key.fileobj)

    def _handle(self, data, addr, bind_ip, sock):
        try:
            p = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if p.get("m") != MAGIC or p.get("id") == self.id:
            return
        now = time.time()
        with self.lock:
            r = self.seen.setdefault(p["id"], {"name": p.get("name"), "heard": {}, "replies": {}, "tcp": {},
                                               "first_seen": now})
            r["name"] = p.get("name")
            r["last"] = now
            r["first_ip"] = p.get("first_ip")
            r["addr"] = addr[0]
            r.setdefault("addrs", set()).add(addr[0])
            r["they_see_us_by"] = p.get("sees", {}).get(self.id, [])
            if p.get("t") == "probe":
                h = r["heard"].setdefault(p["method"], {"count": 0})
                h.update(count=h["count"] + 1, last=now, frm=f"{addr[0]}:{addr[1]}",
                         on=self._iface_for(addr[0], bind_ip))
            elif p.get("t") == "reply":
                r["replies"][p["method"]] = {"rtt_ms": round((now - p.get("echo_ts", now)) * 1000, 1),
                                             "last": now}
        if p.get("t") == "probe":
            try:
                reply = self._packet("reply", p["method"], {"echo_ts": p.get("ts")})
                sock.sendto(reply, addr)
            except OSError:
                pass

    def _tcp_loop(self):
        while not self.stop_ev.wait(3):
            with self.lock:
                todo = [(pid, set(r.get("addrs", ())) | {r.get("first_ip")}) for pid, r in self.seen.items()
                        if time.time() - r.get("last", 0) < PEER_EXPIRE]
            for pid, ips in todo:
                for ip in ips - {None}:
                    with self.lock:
                        prev = self.seen[pid]["tcp"].get(ip)
                    if prev and time.time() - prev["at"] < TCP_EVERY:
                        continue
                    res = self._tcp_try(ip, pid)
                    with self.lock:
                        self.seen[pid]["tcp"][ip] = res

    def _tcp_try(self, ip, pid):
        t0 = time.time()
        try:
            with socket.create_connection((ip, self.target_port), timeout=3) as c:
                line = c.makefile().readline()
            ok = json.loads(line).get("id") == pid
            return {"ok": ok, "ms": round((time.time() - t0) * 1000, 1), "at": time.time(),
                    "note": "" if ok else "a different program answered"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "at": time.time()}

    # -- report
    def report(self):
        now = time.time()
        with self.lock:
            peers = []
            for pid, r in self.seen.items():
                age = now - r.get("last", 0)
                peers.append({
                    "id": pid, "name": r["name"], "online": age < PEER_EXPIRE, "last_heard_s": round(age, 1),
                    "their_sender_address": r.get("addr"), "their_addresses": sorted(r.get("addrs", ())),
                    "their_first_ip": r.get("first_ip"),
                    "we_heard_them_by": {m: {"count": h["count"], "from": h["frm"], "on_our_adapter": h["on"],
                                             "last_s": round(now - h["last"], 1)} for m, h in r["heard"].items()},
                    "they_heard_us_by": r.get("they_see_us_by", []),
                    "answers_we_got": {m: v["rtt_ms"] for m, v in r["replies"].items() if now - v["last"] < PEER_EXPIRE},
                    "tcp": {ip: {k: v for k, v in t.items() if k != "at"} for ip, t in r["tcp"].items()},
                })
            me = {"id": self.id, "name": self.name, "port": self.port, "target_port": self.target_port,
                  "running_s": round(now - self.started), "bound": [f"{k} {ip}" for k, ip in self.bound],
                  "bind_errors": self.bind_errors, "adapters": self.ifs, "first_ips": first_ips(),
                  "broadcast_goes_out_from": broadcast_source(), "sent": self.sent,
                  "static_peers": self.peers}
        rep = {"me": me, "peers_seen": len([p for p in peers if p["online"]]), "peers": peers}
        rep["verdict"] = verdict(rep)
        return rep


def verdict(rep):
    out = []
    me = rep["me"]
    vpn = [i for i in me["adapters"] if i["vpn"]]
    vip = vpn[0]["ip"] if vpn else None
    if me["bind_errors"]:
        out.append("Couldn't open: " + "; ".join(me["bind_errors"]) + " (is a game or another copy using the port?)")
    if not vip:
        out.append("No VPN adapter is up here; only the home network is being tested.")
    elif me["broadcast_goes_out_from"] not in (None, vip):
        out.append(f"Plain broadcasts leave through {me['broadcast_goes_out_from']}, not the VPN ({vip}).")
    if me["first_ips"] and vip and me["first_ips"][0] != vip:
        out.append(f"This PC's first IP is {me['first_ips'][0]}; a game that auto-detects its address would "
                   f"advertise that instead of {vip}.")
    online = [p for p in rep["peers"] if p["online"]]
    if not online:
        out.append(f"Heard no other copy yet. Is it running with the same port ({me['target_port']})? If you "
                   "used --peer and still hear nothing, the firewall is blocking inbound UDP on this port.")
        return out
    for p in online:
        heard = p["we_heard_them_by"]
        bc = [m for m in heard if not m.startswith("unicast")]
        tag = f"{p['name']} ({p['their_sender_address']})"
        if "limited-any" in heard:
            out.append(f"{tag}: their plain broadcast reaches us. Games that broadcast like this will see them.")
        elif bc:
            out.append(f"{tag}: only some broadcasts reach us ({', '.join(sorted(bc))}); their plain "
                       "255.255.255.255 does not, so most games won't see them. Their VPN metric is the likely cause.")
        else:
            out.append(f"{tag}: no broadcast of theirs reaches us, only direct packets. Broadcasts aren't crossing "
                       "(their adapter choice, or our firewall).")
        if "limited-any" not in p["they_heard_us_by"]:
            out.append(f"{tag}: they don't hear our plain broadcast, so games on this PC won't show up for them.")
        fip = p.get("their_first_ip")
        t = p["tcp"].get(fip) if fip else None
        if t and not t.get("ok"):
            out.append(f"{tag}: their first IP {fip} isn't reachable by TCP from here; a game that advertises "
                       "it (auto-detected local address) can be seen but not joined.")
        elif fip and fip == p["their_sender_address"] and t and t.get("ok"):
            out.append(f"{tag}: TCP to their advertised address works; joining should work.")
    return out


def summary_text(rep):
    lines = [f"lanprobe {VERSION}  me={rep['me']['name']} port={rep['me']['port']}  peers seen: {rep['peers_seen']}"]
    for p in rep["peers"]:
        if not p["online"]:
            continue
        lines.append(f"  {p['name']} ({p['their_sender_address']}, first IP {p['their_first_ip']})")
        lines.append("    we hear:   " + (", ".join(sorted(p["we_heard_them_by"])) or "-"))
        lines.append("    they hear: " + (", ".join(sorted(p["they_heard_us_by"])) or "-"))
        lines.append("    tcp:       " + (", ".join(f"{ip} {'ok' if t.get('ok') else 'FAIL'}"
                                                    for ip, t in p["tcp"].items()) or "-"))
    lines += ["  * " + v for v in rep["verdict"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- firewall helper

def firewall(action, port):
    """Add/remove an allow rule for this python.exe on UDP+TCP <port> (needs admin)."""
    if not IS_WIN:
        return {"ok": False, "error": "Windows only"}
    res = []
    for proto in ("UDP", "TCP"):
        name = f"lanprobe {proto} {port}"
        if action == "allow":
            cmd = ["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow",
                   f"program={sys.executable}", f"protocol={proto}", f"localport={port}", "profile=any"]
        else:
            cmd = ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        res.append({"rule": name, "code": r.returncode, "out": (r.stdout + r.stderr).strip()})
    return {"ok": all(x["code"] == 0 for x in res), "results": res,
            "note": "Needs an elevated (admin) process." if any(x["code"] for x in res) else ""}


# ---------------------------------------------------------------- MCP server

TOOLS = [
    {"name": "lanprobe_start", "description": "Start (or restart) the probe on this PC: send game-style discovery "
     "packets and answer the other copy. Close any game using the same port first.",
     "inputSchema": {"type": "object", "properties": {
         "port": {"type": "integer", "description": "UDP/TCP port, default 47810 (Dark Reign 2 uses 26214)"},
         "peers": {"type": "array", "items": {"type": "string"}, "description": "addresses to also probe directly"},
         "name": {"type": "string"}}}},
    {"name": "lanprobe_report", "description": "What this copy has seen: peers, which packet kinds got through in "
     "each direction, TCP reachability and a plain-language verdict.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "lanprobe_stop", "description": "Stop the probe.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "lanprobe_diagnose", "description": "Adapters, metrics, which adapter broadcasts leave from, the PC's "
     "first IP, network profiles and firewall state. Sends no packets.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "lanprobe_firewall", "description": "Add or remove an inbound allow rule for the probe's port "
     "(needs the MCP server to run elevated).",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["allow", "remove"]},
                                                       "port": {"type": "integer"}}, "required": ["action"]}},
]


def mcp_main():
    engine = None
    out = sys.stdout

    def send(msg):
        out.write(json.dumps(msg) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if mid is None:
            continue  # notification
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "lanprobe", "version": VERSION}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if name == "lanprobe_start":
                    if engine:
                        engine.stop()
                    engine = Engine(port=int(args.get("port") or DEFAULT_PORT), peers=args.get("peers") or [],
                                    name=args.get("name")).start()
                    time.sleep(0.5)
                    data = {"started": True, "me": engine.report()["me"]}
                elif name == "lanprobe_report":
                    data = engine.report() if engine else {"error": "not running; call lanprobe_start"}
                elif name == "lanprobe_stop":
                    if engine:
                        engine.stop()
                    engine = None
                    data = {"stopped": True}
                elif name == "lanprobe_diagnose":
                    data = diagnose()
                elif name == "lanprobe_firewall":
                    data = firewall(args.get("action", "allow"),
                                    int(args.get("port") or (engine.port if engine else DEFAULT_PORT)))
                else:
                    raise ValueError(f"unknown tool {name}")
                result = {"content": [{"type": "text", "text": json.dumps(data, indent=1)}]}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:  # noqa: BLE001
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                                                          "isError": True}})
    if engine:
        engine.stop()


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="Game-style LAN discovery tester (run one copy on each PC).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--target-port", type=int, help=argparse.SUPPRESS)  # testing two copies on one PC
    ap.add_argument("--peer", action="append", default=[])
    ap.add_argument("--name")
    ap.add_argument("--seconds", type=float, help="run this long, print the report, exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--mcp", action="store_true")
    ap.add_argument("--include-loopback", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.mcp:
        return mcp_main()
    if a.diagnose:
        print(json.dumps(diagnose(), indent=1))
        return
    e = Engine(port=a.port, target_port=a.target_port, peers=a.peer, name=a.name,
               include_loopback=a.include_loopback).start()
    try:
        if a.seconds:
            time.sleep(a.seconds)
            rep = e.report()
            print(json.dumps(rep, indent=1) if a.json else summary_text(rep))
            return
        print(f"lanprobe running on port {a.port} as '{e.name}'. Run it on the other PC too. Ctrl+C stops.")
        while True:
            time.sleep(5)
            print("\n" + summary_text(e.report()), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        e.stop()


if __name__ == "__main__":
    main()
