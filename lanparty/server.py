"""HTTP server. /peer/* is for other launchers on the LAN; /api/* and the UI answer only this PC.

When an internet party is active, /peer/* requests must be signed (HMAC-SHA256). See security.py."""
import hashlib
import json
import shutil
import mimetypes
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION, compress, forcebind, handoff, launcher, media, prereqs, regfix, throttle, update
from . import vpn as vpn_mod
from .defender import defender_status, exclude_folders
from .discovery import local_ipv4s
from .netwrap import safe_netsh
from .peers import probe_peer, best_ip, link_report
from .transfer import Uploads
from .security import PartyAuth, make_party_secret, make_party_id
from .vpn import VPN, encode_party_code, decode_party_code, download_n2n, install_tap_driver, tap_installed, n2n_ready, n2n_bundled
from .vpn import ensure_tap_adapter, is_admin, ADMIN_NEEDED, SUPERNODE_PORT, VPN_SUBNET, release_n2n_for_handoff

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
LOCAL_HOSTS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def write_lan_json(game_dir: str, title: str, exe: str, args: str) -> str:
    """Put exe / args into <game>\\lan.json so they travel with the game. Other keys are kept, an existing file is
    backed up first (lan.json.bak-<date_time>), and an empty value removes its key. 'ok' or the problem."""
    p = os.path.join(game_dir, "lan.json")
    raw = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return "The game's lan.json isn't valid JSON, so it wasn't changed. Fix or remove it first."
        if not isinstance(raw, dict):
            return "The game's lan.json isn't a JSON object, so it wasn't changed."
        try:
            shutil.copy2(p, p + ".bak-" + time.strftime("%Y-%m-%d_%H%M%S"))
        except OSError as e:
            return f"Couldn't back up lan.json: {e}"
    raw.setdefault("game", title)
    for k, v in (("exe", exe), ("args", args)):
        if v:
            raw[k] = v
        else:
            raw.pop(k, None)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
    except OSError as e:
        return f"Couldn't write lan.json: {e}"
    return "ok"


class App:
    """Holds every subsystem; the handler reaches it as self.server.app."""

    def __init__(self, cfg, library, discovery, sync, votes, downloads, net):
        self.cfg, self.library, self.discovery = cfg, library, discovery
        self.sync, self.votes, self.downloads, self.net = sync, votes, downloads, net
        self.installer = prereqs.Installer(cfg)
        self.party = None
        self.notes = []
        self._pre_cache = (0.0, {})
        self.vpn = VPN(cfg.data_dir)
        self.vpn.game_exes = lambda: [p for g in list(library.games.values()) for p in launcher.game_exe_paths(g)]
        self.auth: PartyAuth = None    # set when an internet party is active
        self.updater = update.Updater(self)
        self.uploads = Uploads(discovery)           # what peers are copying from us, for the game cards
        throttle.configure(cfg, library, net)       # play-time bandwidth limit
        discovery.downloading = downloads.downloading   # swarm hint in our hello
        # Party Master: a poll we haven't seen is only taken in when the player who started it is the Party Master
        # right now (its hello says so; for our own polls, our setting). Ballots on known polls come from everyone.
        votes.poll_gate = discovery.is_party_master

    def open_blocked_for(self, game: dict) -> None:
        """During an internet party, turn off firewall block rules for the game being started (in the background:
        checking the firewall takes a few seconds)."""
        if self.vpn.is_active():
            threading.Thread(target=self.vpn.open_blocked_for, args=(launcher.game_exe_paths(game),),
                             daemon=True).start()

    def my_game_ip(self) -> str:
        """{my_ip} for a game: the VPN IP while hosting or in an internet party, else "" (launcher uses the LAN IP)."""
        return (getattr(self.vpn, "my_ip", "") or "") if getattr(self.vpn, "state", "") in ("host", "joined") else ""

    def start_party_auth(self, security_state: dict = None) -> None:
        """Signing for the party the VPN is in: our key, outgoing signatures, and polling the VPN for peers."""
        from . import peers as _peers
        self.auth = PartyAuth(self.vpn.party_id, self.vpn.party_secret)
        if security_state:
            self.auth.import_state(security_state)
        self.auth.add_peer(self.cfg["peer_id"], self.cfg["name"])
        _peers.set_auth(self.vpn.party_id, self.auth.active_peers[self.cfg["peer_id"]]["key"], self.cfg["peer_id"])
        port = self.discovery.http_port
        if self.vpn.my_ip:
            self.discovery.add_vpn_peer(self.vpn.my_ip, port)
        if self.vpn.state == "joined":
            self.discovery.add_vpn_peer(f"{VPN_SUBNET}.1", port)   # the host

    def hand_over_party(self) -> bool:
        """Update restart: save the party for the next launcher and let n2n outlive this one. False = can't (then
        the caller leaves the party as before)."""
        if not self.vpn.is_active() or not self.auth:
            return False
        d = self.vpn.snapshot()
        if "edge" not in d["procs"] or (self.vpn.state == "host" and "supernode" not in d["procs"]):
            return False
        d.update(security=self.auth.export_state(), vpn_peers=list(self.discovery._vpn_peers),
                 game_party=self.party)
        if not handoff.save_handoff(self.cfg.data_dir, d):
            return False
        if not release_n2n_for_handoff():
            handoff.take_handoff(self.cfg.data_dir)     # delete it again: n2n would die with us
            return False
        return True

    def resume_party(self, exes: dict = None) -> bool:
        """On start: take over the party the launcher this update replaced handed over. If there's a hand-over
        that can't be used, its n2n processes are stopped (only if they really are ours) and False is returned,
        so the caller undoes the game network settings as after a crash."""
        d = handoff.take_handoff(self.cfg.data_dir)
        if not d:
            return False
        r = self.vpn.resume(d, exes)
        if r != "ok":
            self.vpn.log.append(f"Couldn't stay in the party after the update ({r}); left it. Rejoin it from here.")
            names = exes or {"edge": vpn_mod._edge_path(self.cfg.data_dir),
                             "supernode": vpn_mod._supernode_path(self.cfg.data_dir)}
            for name, ident in (d.get("procs") or {}).items():
                if name in names:
                    handoff.stop_if_ours(ident, names[name])
            return False
        self.start_party_auth(d.get("security"))
        for entry in d.get("vpn_peers") or []:
            try:
                self.discovery.add_vpn_peer(str(entry[0]), int(entry[1]))
            except (TypeError, ValueError, IndexError):
                pass
        if isinstance(d.get("game_party"), dict):
            self.party = d["game_party"]
        self.note("Launcher updated; you're still in the internet party.")
        return True

    def save_game_settings(self, b: dict) -> dict:
        """Gear dialog: exe and extra arguments (this PC, or shared through the game's lan.json), run as admin,
        ForceBindIP. The exe must be one of the programs found inside the game folder (rule 5)."""
        gid = str(b.get("id", ""))
        g = self.library.get(gid)
        if not g:
            return {"ok": False, "error": "You don't have this game."}
        rels = {e["rel"].lower(): e["rel"] for e in g.get("exes", [])}
        exe = str(b.get("exe", "") or "").strip().replace("/", "\\")
        if exe and exe.lower() not in rels:
            return {"ok": False, "error": f"{exe} isn't a program in this game's folder."}
        exe = rels.get(exe.lower(), "")
        args = " ".join(str(b.get("args", "") or "").split())[:500]
        overrides = dict(self.cfg.get("game_overrides", {}) or {})
        msg = "Saved for this PC."
        if b.get("share"):
            r = write_lan_json(g["path"], g["title"], exe, args)
            if r != "ok":
                return {"ok": False, "error": r}
            overrides.pop(gid, None)          # the shared lan.json applies here too
            msg = "Saved in the game's lan.json: other players get it with the game."
        else:
            entry = {k: v for k, v in (("exe", exe), ("args", args)) if v}
            if entry:
                overrides[gid] = entry
            else:
                overrides.pop(gid, None)
        self.cfg.set("game_overrides", overrides)
        if "run_as_admin" in b:
            lst = [x for x in (self.cfg.get("run_as_admin", []) or []) if x != gid]
            self.cfg.set("run_as_admin", lst + ([gid] if b.get("run_as_admin") else []))
        if "force_bind" in b:
            lst = [x for x in (self.cfg.get("no_force_bind", []) or []) if x != gid]
            self.cfg.set("no_force_bind", lst + ([] if b.get("force_bind") else [gid]))
        self.library.rescan_async()
        return {"ok": True, "msg": msg}

    def remember_party(self, **d) -> None:
        handoff.save_last_party(self.cfg.data_dir, d)

    def last_party_summary(self) -> dict:
        """For the Network tab (loopback only): what 'Rejoin' / 'Host again' would use."""
        lp = handoff.last_party(self.cfg.data_dir)
        if not lp:
            return {}
        out = {"role": lp.get("role"), "at": lp.get("at")}
        if lp.get("role") == "joined":
            out["code"] = lp.get("code", "")
        else:
            out["port"] = lp.get("port", SUPERNODE_PORT)
            out["external_ip"] = lp.get("external_ip", "")
        return out

    def note(self, msg: str) -> None:
        self.notes = (self.notes + [{"t": time.time(), "msg": msg}])[-30:]

    def prereq_status(self, force: bool = False) -> dict:
        at, st = self._pre_cache
        if force or time.time() - at > 30 or self.installer.status.get("done", 0) and self.installer.status["done"] > at:
            st = prereqs.check_all()
            self._pre_cache = (time.time(), st)
        return st

    def prereq_report(self) -> dict:
        st = self.prereq_status()
        games = self.library.public_list()
        needed = {}
        for g in games:
            for pid in g.get("prereqs", []):
                needed.setdefault(pid, []).append(g["title"])
        local = {}
        for g in games:
            full = self.library.get(g["id"])
            for r in g.get("redists", []):
                local.setdefault(r["id"], os.path.join(full["path"], r["rel"]))
        items = []
        for pid, spec in prereqs.CATALOG.items():
            items.append({"id": pid, "name": spec["name"], "installed": bool(st.get(pid)),
                          "needed_by": needed.get(pid, []), "local": local.get(pid, ""),
                          "how": "Windows feature" if spec["kind"] == "dism" else
                                 ("bundled installer" if pid in local else ("download" if spec.get("url") else "web page"))})
        return {"items": items, "installer": self.installer.status}

    def _auth_members(self) -> list:
        if not self.auth:
            return []
        names = {p["id"]: p["name"] for p in self.discovery.list()}
        names[self.cfg["peer_id"]] = self.cfg["name"]
        self.auth.learn_names(names)
        return self.auth.members()

    def state(self) -> dict:
        names = {p["id"]: p["name"] for p in self.discovery.list()}
        names[self.cfg["peer_id"]] = self.cfg["name"] + " (you)"
        lib = self.sync.merged_library()
        likes = self.votes.like_summary()
        for g in lib:
            g["like"] = likes.get(g["id"], {"mine": 0, "avg": 0, "n": 0, "voters": []})
            g["owner_names"] = [names.get(o, o) for o in g["owners"]]
            local = self.library.get(g["id"]) if g["have"] else None
            g["setup_needed"] = bool(local) and launcher.setup_needed(local, self.cfg)
            if local:   # ForceBindIP during a party: can apply / on for this PC / turned off by the game's lan.json
                g["force_bind"] = {"applies": local.get("kind") == "pe" and local.get("network") != "ipx",
                                   "on": forcebind.enabled_for(local, self.cfg),
                                   "lan_off": (local.get("lan") or {}).get("force_bind") is False}
                # gear dialog (this PC only): every program in the folder, and the per-PC switches
                g["exes"] = local.get("exes", [])
                g["run_as_admin"] = local["id"] in (self.cfg.get("run_as_admin", []) or [])
        titles = {g["id"]: g["title"] for g in lib}
        polls = self.votes.results()
        for p in polls:
            p["titles"] = {c: titles.get(c, c) for c in p["candidates"]}
        master = bool(self.cfg.get("party_master", False))
        mlist = self.sync.merged_media()
        for m in mlist:
            m["owner_names"] = [names.get(o, o) for o in m["owners"]]
        return {
            "version": VERSION,
            "pid": os.getpid(),
            "update": {"available": self.updater.available(), "status": self.updater.status},
            "me": {"id": self.cfg["peer_id"], "name": self.cfg["name"], "ips": local_ipv4s(), "party_master": master},
            # the Party Master's heart scores (average out of 10 over the players who scored each game)
            "scores": self.votes.scoreboard(titles) if master else None,
            "peers": self.discovery.list(),
            "library": lib,
            "scanning": self.library.scanning,
            "media": mlist,                                  # Movies / TV / Music / Books / Misc (media.py)
            "media_scanning": self.library.media.scanning,
            "media_categories": media.CATEGORIES,
            "downloads": self.downloads.list(),
            "uploads": self.uploads.list(),
            "throttle": throttle.status(),
            "hashing": self.library.hashes.pending,
            "polls": polls,
            "party": self.party,
            "vpn": self.vpn.status(),
            "auth_members": self._auth_members(),
            "last_party": {} if self.vpn.is_active() else self.last_party_summary(),
            "notes": self.notes[-8:],
            "settings": {k: self.cfg.get(k) for k in ("folders", "download_dir", "dosbox_path", "static_peers",
                                                       "share_library", "name", "party_master",
                                                       "limit_while_playing", "limit_mbps", "share_media")}
                     | {"media_folders": self.library.media.folders()},
        }

    def net_report(self) -> dict:
        games = []
        for g in self.library.public_list():
            full = self.library.get(g["id"])
            lan = g.get("lan") or {}
            games.append({"id": g["id"], "title": g["title"], "network": g.get("network", ""), "kind": g.get("kind"),
                          "arch": g.get("arch"), "wrapped": self.net.wrapper_installed(full) if g.get("kind") == "pe" else False,
                          "ports": lan.get("ports", {}),
                          "firewall": [{"cmd": c, "ok": safe_netsh(c)} for c in lan.get("firewall", [])]})
        return {"adapters": self.net.adapters(), "ipxwrapper_ready": self.net.ipxwrapper_ready(),
                "dosbox": self.net.dosbox_path(), "games": games, "log": self.net.log[-30:]}


def pick_folder(title: str = "Choose a folder that holds your games") -> str:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title=title)
    root.destroy()
    return os.path.normpath(path) if path else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "LanParty/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    @property
    def app(self) -> App:
        return self.server.app

    # ---- helpers ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self) -> dict:
        # if the peer auth already read the body, use the cached copy
        if hasattr(self, "_cached_body"):
            raw = self._cached_body
            del self._cached_body
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                return {}
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _is_local(self) -> bool:
        if self.client_address[0] not in LOCAL_HOSTS:
            return False
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")   # blocks DNS-rebinding pages

    # ---- routing ----
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)
        try:
            if path.startswith("/peer/"):
                return self._peer(method, path, qs)
            if not self._is_local():
                return self._json({"error": "local only"}, 403)
            if path.startswith("/api/"):
                if method == "POST" and self.headers.get("X-LanParty") != "1":
                    return self._json({"error": "missing header"}, 403)
                return self._api(method, path, qs)
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.realpath(os.path.join(WEB, rel))
        if os.path.commonpath([os.path.realpath(WEB), full]) != os.path.realpath(WEB) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, mimetypes.guess_type(full)[0] or "application/octet-stream")

    # ---- LAN peers ----
    def _peer(self, method: str, path: str, qs: dict) -> None:
        a = self.app
        parts = path.split("/")

        # When an internet party is active, verify the request signature.
        # /peer/hello is exempt so new peers can discover us before they've authenticated.
        if a.auth and path != "/peer/hello":
            body = b""
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 4 * 1024 * 1024:
                body = self.rfile.read(n)
                # stash the body so _body() can still use it
                self._cached_body = body
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            ok, peer_id, reason = a.auth.verify_request(
                self.client_address[0], method, path, body, hdrs)
            if not ok:
                return self._send(403, b"", "text/plain")

        if path == "/peer/hello":
            return self._json(a.discovery.hello())
        if path == "/peer/votes":
            if method == "POST":
                a.votes.merge(self._body())
                return self._json({"ok": True})
            return self._json(a.votes.state())
        if path == "/peer/party" and method == "POST":
            b = self._body()
            host = a.discovery.get(str(b.get("host_id")))
            a.party = {"game_id": str(b.get("game_id")), "title": str(b.get("title", ""))[:80],
                       "host_id": str(b.get("host_id")), "host_name": str(b.get("host_name", ""))[:40],
                       "host_ip": host["ip"] if host else self.client_address[0], "at": time.time()}
            if b.get("clear"):
                a.party = None
            return self._json({"ok": True})
        if path == "/peer/app/release" and method == "GET":
            # this launcher's own code (no bin, settings or backups); a peer installs it only when its user clicks Update
            try:
                return self._serve_file(update.current_release())
            except OSError as e:
                return self._json({"error": f"no release: {e}"}, 500)
        if not a.cfg.get("share_library", True):
            return self._json({"error": "sharing off"}, 403)
        if path == "/peer/library":
            return self._json(a.library.public_list())
        if path == "/peer/media":
            return self._json(a.library.media.public_list() if a.cfg.get("share_media", True) else [])
        if len(parts) == 4 and media.is_media_id(urllib.parse.unquote(parts[3])) and not a.cfg.get("share_media", True):
            return self._json({"error": "media sharing off"}, 403)
        if len(parts) == 4 and parts[2] == "manifest":
            man = a.library.manifest(urllib.parse.unquote(parts[3]))
            return self._json(man) if man else self._json({"error": "no such game"}, 404)
        if len(parts) == 4 and parts[2] == "swarm":
            # what we have so far of a game we're downloading, for other players fetching it too
            sw = a.downloads.swarm(urllib.parse.unquote(parts[3]))
            return self._json(sw.info()) if sw else self._json({"error": "not downloading"}, 404)
        if len(parts) == 4 and parts[2] == "file":
            gid, rel = urllib.parse.unquote(parts[3]), (qs.get("p") or [""])[0]
            full = a.library.resolve_file(gid, rel)
            if full:
                return self._serve_file(full, gid)
            sw = a.downloads.swarm(gid) if not a.library.has(gid) else None
            r = sw.reader(rel) if sw else None
            if not r:
                return self._json({"error": "no such file"}, 404)
            return self._serve_range(r[0], r[1], [rel], gid)
        if len(parts) == 4 and parts[2] == "bundle" and method == "POST":
            return self._serve_bundle(urllib.parse.unquote(parts[3]), self._body())
        return self._json({"error": "unknown"}, 404)

    @staticmethod
    def _file_reader(full: str):
        def read(start, length):
            with open(full, "rb") as f:
                f.seek(start)
                return f.read(length)
        return read

    def _serve_file(self, full: str, gid: str = "") -> None:
        self._serve_range(os.path.getsize(full), self._file_reader(full), [full], gid)

    def _serve_range(self, size: int, read, names: list, gid: str = "") -> None:
        """One file (a game file, a part-downloaded one for the swarm, or our release zip), honouring Range.
        read(start, length) -> bytes, or None when the swarm no longer has those bytes."""
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].split(",")[0].partition("-")
                start = int(a or 0)
                if b:
                    end = min(int(b), size - 1)
            except ValueError:
                start, end = 0, size - 1
        start = min(start, size)
        length = max(0, end - start + 1)
        partial = start > 0 or end < size - 1
        data, packed, codec = None, None, ""
        if self.command != "HEAD" and length <= compress.MAX_UNIT:
            data = read(start, length)          # read up front: lets us compress it, and the swarm say no
            if data is None:
                return self._json({"error": "not here yet"}, 404)
            if len(data) != length:             # file shrank under us: drop the connection, the client retries
                self.close_connection = True
                raise ConnectionAbortedError("file changed while sending")
            codec = compress.pick(self.headers)
            if compress.MIN_UNIT <= length and codec and compress.worth_trying(names):
                t = time.perf_counter()
                packed = compress.compress(data, codec, self._zstd_level())
                ctime = time.perf_counter() - t
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(packed) if packed is not None else length))
        if packed is not None:
            self.send_header(compress.ENCODING_HEADER, codec)
            self.send_header(compress.RAW_LENGTH_HEADER, str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{start + length - 1}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command == "HEAD":
            return
        if data is not None:
            if packed is not None:
                self._send_packed(codec, packed, ctime)
            else:
                throttle.write(self.wfile, data)
            self._count_upload(gid, len(packed) if packed is not None else length, length)
            return
        left, pos = length, start               # a big reply (whole file to an older launcher): stream it
        while left > 0:
            chunk = read(pos, min(1 << 20, left))
            if not chunk:          # file shrank under us: drop the connection so the client sees a short read
                self.close_connection = True
                raise ConnectionAbortedError("file changed while sending")
            throttle.write(self.wfile, chunk)
            self._count_upload(gid, len(chunk), len(chunk))
            left -= len(chunk)
            pos += len(chunk)

    def _zstd_level(self) -> int:
        """This downloader's current zstd level (compress.Adaptive: starts 3 on the LAN, 9 over the VPN/internet)."""
        return compress.ADAPT.level(self.client_address[0], bool(throttle.UP.rate()))

    def _send_packed(self, codec: str, body: bytes, compress_s: float) -> None:
        """Write a compressed body and tell the level adapter how long compressing vs sending took."""
        t = time.perf_counter()
        throttle.write(self.wfile, body)
        if codec == "zstd" and compress_s > 0:
            compress.ADAPT.feedback(self.client_address[0], compress_s, time.perf_counter() - t,
                                    bool(throttle.UP.rate()))

    def _count_upload(self, gid: str, wire: int, raw: int) -> None:
        if gid and getattr(self.app, "uploads", None) is not None:
            self.app.uploads.add(gid, self.client_address[0], wire, raw, str(self.headers.get("X-LP-From") or ""))

    BUNDLE_MAX_FILES = 512
    BUNDLE_MAX_BYTES = 64 << 20

    def _serve_bundle(self, gid: str, body: dict) -> None:
        """Many small files in one reply, so a game of thousands of small files isn't one round trip per file.
        Reply: one JSON line {"sizes": [...]} (-1 = not here), then the files' bytes back to back in that order.
        Files come from our library, or (a game we're still downloading) the ones that have fully arrived."""
        names = [str(p) for p in (body.get("files") or [])][:self.BUNDLE_MAX_FILES]
        lib_has = self.app.library.has(gid)
        swarm = None if lib_has else self.app.downloads.swarm(gid)
        readers, sizes, total = [], [], 0
        for rel in names:
            size, read = -1, None
            if lib_has:
                full = self.app.library.resolve_file(gid, rel)
                if full:
                    size, read = os.path.getsize(full), self._file_reader(full)
            elif swarm is not None:
                r = swarm.reader(rel)
                if r is not None and (swarm.states.get(rel) is None or swarm.states[rel].finished):
                    size, read = r
            if size >= 0 and total + size > self.BUNDLE_MAX_BYTES:
                size, read = -1, None           # over the cap: the client fetches it on its own
            readers.append(read)
            sizes.append(size)
            total += max(size, 0)
        datas = [] if total <= compress.MAX_UNIT else None    # a big bundle isn't held in memory: streamed below
        for read, size in zip(readers, sizes if datas is not None else []):
            if size <= 0:                       # read before the reply starts: a file that changed is a retry
                continue
            d = read(0, size)
            if d is None or len(d) != size:
                self.close_connection = True
                raise ConnectionAbortedError("file changed while sending")
            datas.append(d)
        head = (json.dumps({"sizes": sizes}) + "\n").encode("utf-8")
        packed = None
        if datas is not None:
            data = head + b"".join(datas)
            codec = compress.pick(self.headers)
            if codec and compress.worth_trying(names):
                t = time.perf_counter()
                packed = compress.compress(data, codec, self._zstd_level())
                ctime = time.perf_counter() - t
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if packed is not None:
            self.send_header(compress.ENCODING_HEADER, codec)
            self.send_header(compress.RAW_LENGTH_HEADER, str(len(data)))
        self.send_header("Content-Length", str(len(packed) if packed is not None else len(head) + total))
        self.end_headers()
        if datas is not None:
            if packed is not None:
                self._send_packed(codec, packed, ctime)
            else:
                throttle.write(self.wfile, data)
            self._count_upload(gid, len(packed) if packed is not None else len(data), len(data))
            return
        throttle.write(self.wfile, head)
        for read, size in zip(readers, sizes):
            if size <= 0:
                continue
            pos = 0
            while pos < size:
                chunk = read(pos, min(1 << 20, size - pos))
                if not chunk:      # file shrank under us: drop the connection so the client sees a short read
                    self.close_connection = True
                    raise ConnectionAbortedError("file changed while sending")
                throttle.write(self.wfile, chunk)
                self._count_upload(gid, len(chunk), len(chunk))
                pos += len(chunk)

    # ---- local UI ----
    def _api(self, method: str, path: str, qs: dict) -> None:
        a = self.app
        b = self._body() if method == "POST" else {}
        if path == "/api/state":
            return self._json(a.state())
        if path == "/api/prereqs":
            if qs.get("force"):
                a.prereq_status(force=True)
            return self._json(a.prereq_report())
        if path == "/api/net":
            return self._json(a.net_report())
        if path == "/api/defender/status":
            return self._json(defender_status(a.cfg["folders"]))
        if path == "/api/links":
            peers = a.discovery.list()
            cached = link_report()
            links = []
            for p in peers:
                c = cached.get(p["id"], {})
                links.append({"id": p["id"], "name": p["name"], "ips": p.get("ips", [p["ip"]]),
                               "best_ip": c.get("ip", p["ip"]), "probes": c.get("probes", {})})
            return self._json({"links": links})
        if method != "POST":
            return self._json({"error": "unknown"}, 404)

        if path == "/api/settings":
            for k in ("name", "download_dir", "dosbox_path"):
                if k in b:
                    a.cfg.set(k, str(b[k]).strip()[:260])
            if "static_peers" in b:
                a.cfg.set("static_peers", [s.strip() for s in b["static_peers"] if s.strip()][:50])
            if "share_library" in b:
                a.cfg.set("share_library", bool(b["share_library"]))
            if "share_media" in b:
                a.cfg.set("share_media", bool(b["share_media"]))
                a.sync.refresh_libraries_soon()
            if "limit_while_playing" in b:
                a.cfg.set("limit_while_playing", b["limit_while_playing"] is True)
            if "limit_mbps" in b:
                try:
                    a.cfg.set("limit_mbps", min(1000.0, max(0.1, round(float(b["limit_mbps"]), 1))))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "The limit must be a number of MB/s"})
            if "party_master" in b:     # peers learn it from our next hello (every 2 s)
                a.cfg.set("party_master", b["party_master"] is True)
            return self._json({"ok": True})
        if path == "/api/folders/add":
            p = pick_folder() if b.get("pick") else os.path.normpath(str(b.get("path", "")).strip().strip('"'))
            if not p or not os.path.isdir(p):
                return self._json({"ok": False, "error": "Not a folder" if p else "Cancelled"})
            if p not in a.cfg["folders"]:
                a.cfg.set("folders", a.cfg["folders"] + [p])
            a.library.rescan_async()
            return self._json({"ok": True, "path": p})
        if path == "/api/folders/remove":
            a.cfg.set("folders", [f for f in a.cfg["folders"] if f != b.get("path")])
            a.library.rescan_async()
            return self._json({"ok": True})
        if path == "/api/rescan":
            a.library.rescan_async()
            a.library.media.rescan_async()
        if path == "/api/media/folder":
            # the folder for one media category: Browse (Windows folder picker), a pasted path, or clear
            cat = str(b.get("cat", ""))
            if cat not in media.CATEGORIES:
                return self._json({"ok": False, "error": "Unknown category"})
            label = media.CATEGORIES[cat]
            if b.get("clear"):
                a.library.media.set_folder(cat, "")
                a.library.media.rescan_async()
                return self._json({"ok": True})
            p = pick_folder(f"Choose the folder for your {label}") if b.get("pick") else \
                os.path.normpath(str(b.get("path", "")).strip().strip('"')) if str(b.get("path", "")).strip() else ""
            if not p:
                return self._json({"ok": False, "error": "Cancelled" if b.get("pick") else "Paste a folder path"})
            if not os.path.isdir(p):
                return self._json({"ok": False, "error": f"{p} is not a folder"})
            a.library.media.set_folder(cat, p)
            a.library.media.rescan_async()
            return self._json({"ok": True, "path": p})
        if path == "/api/media/show":
            # Show in folder: opens Explorer at the item (never runs the file: it may have come from someone else)
            m = a.library.media.get(str(b.get("id", "")))
            if not m:
                return self._json({"ok": False, "error": "You don't have this item."})
            if os.name != "nt":
                return self._json({"ok": False, "error": m["path"]})
            import subprocess
            if m.get("single"):
                subprocess.Popen(["explorer", "/select,", m["path"]])
            else:
                subprocess.Popen(["explorer", m["path"]])
            return self._json({"ok": True})
            return self._json({"ok": True})
        if path == "/api/vote/new":
            if not a.cfg.get("party_master", False):
                return self._json({"ok": False, "error": "Only the Party Master can start a poll "
                                                         "(Settings → Party Master)."})
            lib_ids = [g["id"] for g in a.sync.merged_library()]
            if isinstance(b.get("candidates"), list):     # the games the Party Master ticked, in the library only
                known = set(lib_ids)
                cands = [c for c in dict.fromkeys(str(x) for x in b["candidates"]) if c in known]
            else:
                cands = lib_ids
            if not cands:
                return self._json({"ok": False, "error": "Pick at least one game for the poll."
                                   if isinstance(b.get("candidates"), list) else "No games to vote on yet."})
            return self._json({"ok": True, "id": a.votes.new_poll(str(b.get("title", "")), cands)})
        if path == "/api/vote/ballot":
            a.votes.cast(str(b.get("poll")), [str(x) for x in b.get("ranking", [])])
            return self._json({"ok": True})
        if path == "/api/like":
            # hearts: this player's interest in a game (0-10); peers get it with the poll state on /peer/votes
            gid = str(b.get("id", ""))[:80]
            if not gid:
                return self._json({"ok": False, "error": "No game"})
            return self._json({"ok": True, "score": a.votes.like(gid, b.get("score", 0))})
        if path == "/api/vote/close":
            a.votes.close(str(b.get("poll")))
            return self._json({"ok": True})
        if path == "/api/download":
            gid = str(b.get("id"))
            pool = a.sync.merged_media() if media.is_media_id(gid) else a.sync.merged_library()
            g = next((x for x in pool if x["id"] == gid), None)
            if not g:
                return self._json({"ok": False, "error": "Unknown game"})
            if media.is_media_id(gid) and g.get("have"):
                return self._json({"ok": False, "error": "You already have it."})
            owners = [o for o in g["owners"] if o != a.cfg["peer_id"]]
            return self._json({"ok": True, "msg": a.downloads.start(gid, owners)})
        if path == "/api/launch":
            g = a.library.get(str(b.get("id")))
            if not g:
                return self._json({"ok": False, "error": "You don't have this game yet."})
            notes, info = [], []
            a.open_blocked_for(g)
            r = launcher.launch(g, a.net, str(b.get("role", "solo")), str(b.get("host_ip", "")), a.cfg["name"], a.cfg,
                                notes, info, a.my_game_ip())
            for n in notes:
                a.note("Name not set: " + n)
            for n in info:
                a.note(n)
            return self._json({"ok": r == "ok", "error": "" if r == "ok" else r, "warning": "; ".join(notes)})
        if path == "/api/game/settings":
            return self._json(a.save_game_settings(b))
        if path in ("/api/game/open_folder", "/api/game/reg_import", "/api/game/firewall"):
            g = a.library.get(str(b.get("id", "")))
            if not g:
                return self._json({"ok": False, "error": "You don't have this game."})
            if path.endswith("open_folder"):
                try:
                    os.startfile(g["path"])        # Explorer on this PC; nothing runs from the folder
                except (OSError, AttributeError) as e:
                    return self._json({"ok": False, "error": f"Couldn't open the folder: {e}"})
                return self._json({"ok": True})
            if path.endswith("reg_import"):
                if not regfix.find_reg_files(g["path"]):
                    return self._json({"ok": False, "error": "No .reg file in this game's folder."})
                threading.Thread(target=lambda: (regfix.import_reg_files(a.cfg, g, a.note),
                                                 a.note(f"{g['title']}: registry import finished.")), daemon=True).start()
                return self._json({"ok": True, "admin": is_admin()})

            def fw() -> None:
                ok = a.net.open_firewall([g], a.net._log)
                a.note(f"Firewall rules added for {g['title']}." if ok else
                       f"Firewall rules for {g['title']} were not added: see the log on the Network tab.")
            threading.Thread(target=fw, daemon=True).start()
            return self._json({"ok": True, "admin": is_admin()})
        if path == "/api/game/force_bind":
            # per-PC switch (settings no_force_bind); lan.json "force_bind": false can't be overridden from here
            gid = str(b.get("id", ""))
            if not a.library.get(gid):
                return self._json({"ok": False, "error": "Unknown game"})
            off = [x for x in (a.cfg.get("no_force_bind", []) or []) if x != gid]
            if not b.get("on"):
                off.append(gid)
            a.cfg.set("no_force_bind", off)
            return self._json({"ok": True})
        if path == "/api/setup":
            g = a.library.get(str(b.get("id")))
            if not g:
                return self._json({"ok": False, "error": "Unknown game"})
            r = launcher.run_setup(g, a.cfg, lambda gm, code: a.note(
                f"{gm['title']} setup {'finished' if code == 0 else f'failed (exit code {code})'}"))
            return self._json({"ok": r == "ok", "error": "" if r == "ok" else r})
        if path == "/api/party/start":
            g = a.library.get(str(b.get("id")))
            if not g:
                return self._json({"ok": False, "error": "You need the game to host it."})
            ips = local_ipv4s()
            notes, info = [], []
            a.open_blocked_for(g)
            r = launcher.launch(g, a.net, "host", ips[0] if ips else "127.0.0.1", a.cfg["name"], a.cfg, notes, info,
                                a.my_game_ip())
            if r != "ok":
                return self._json({"ok": False, "error": r})
            for n in info:
                a.note(n)
            warn = "; ".join("Name not set: " + n for n in notes)
            pl = (g.get("lan") or {}).get("players")
            if pl and len(a.discovery.list()) + 1 < pl["min"]:
                warn += f" {g['title']} needs at least {pl['min']} players; {len(a.discovery.list()) + 1} online."
            elif pl and len(a.discovery.list()) + 1 > pl["max"]:
                warn += f" {g['title']} takes at most {pl['max']} players."
            a.party = {"game_id": g["id"], "title": g["title"], "host_id": a.cfg["peer_id"],
                       "host_name": a.cfg["name"], "host_ip": ips[0] if ips else "", "at": time.time(), "me": True}
            a.sync.broadcast("/peer/party", {"game_id": g["id"], "title": g["title"],
                                             "host_id": a.cfg["peer_id"], "host_name": a.cfg["name"]})
            return self._json({"ok": True, "warning": warn})
        if path == "/api/party/clear":
            if a.party and a.party.get("me"):
                a.sync.broadcast("/peer/party", {"clear": True, "host_id": a.cfg["peer_id"]})
            a.party = None
            return self._json({"ok": True})
        if path == "/api/prereqs/install":
            rep = a.prereq_report()
            want = set(b.get("ids") or [i["id"] for i in rep["items"] if i["needed_by"] and not i["installed"]])
            items = [{"id": i["id"], "local": i["local"] or None} for i in rep["items"] if i["id"] in want]
            if not items:
                return self._json({"ok": False, "error": "Nothing to install."})
            return self._json({"ok": a.installer.start(items)})
        if path == "/api/net/fetch_ipxwrapper":
            r = a.net.fetch_ipxwrapper()
            return self._json({"ok": r == "ok", "error": r})
        if path == "/api/net/wrap":
            g = a.library.get(str(b.get("id")))
            if not g:
                return self._json({"ok": False, "error": "Unknown game"})
            r = a.net.install_wrapper(g) if b.get("on") else a.net.remove_wrapper(g)
            return self._json({"ok": r == "ok", "error": r})
        if path == "/api/net/firewall":
            games = [a.library.get(g["id"]) for g in a.library.public_list()]

            def fw() -> None:
                ok = a.net.open_firewall(games, a.net._log)
                a.note(f"Firewall rules added for the launcher and {len(games)} game(s)." if ok else
                       "Firewall rules were not added: see the log at the bottom of the Network tab.")
            threading.Thread(target=fw, daemon=True).start()
            return self._json({"ok": True, "admin": is_admin()})
        # ---- VPN / internet party ----
        if path == "/api/vpn/status":
            return self._json(a.vpn.status())
        if path == "/api/vpn/download_n2n":
            r = download_n2n(a.cfg.data_dir, a.vpn.log)
            return self._json({"ok": r == "ok", "error": r})
        if path == "/api/vpn/install_tap":
            if not is_admin():
                return self._json({"ok": False, "error": ADMIN_NEEDED})
            r = ensure_tap_adapter(a.cfg.data_dir, a.vpn.log)
            return self._json({"ok": r == "ok", "error": r})
        if path == "/api/vpn/create_party":
            ext_ip = str(b.get("external_ip", "")).strip()
            port = int(b.get("port", 7654))
            secret = None
            if b.get("again"):        # "Host again": the last party's secret, so its code still works
                lp = handoff.last_party(a.cfg.data_dir) or {}
                if lp.get("role") != "host":
                    return self._json({"ok": False, "error": "No recent party to host again."})
                secret = bytes.fromhex(lp.get("secret", ""))
                if "external_ip" not in b:
                    ext_ip = str(lp.get("external_ip", ""))
                if "port" not in b:
                    port = int(lp.get("port") or 7654)
            result = a.vpn.create_party(ext_ip, port, secret)
            if result.startswith("already") or result.startswith("Could") or result.startswith("TAP") or result.startswith("n2n") or result.startswith("supernode") or result.startswith("edge"):
                return self._json({"ok": False, "error": result})
            # result is the party code
            a.start_party_auth()
            a.remember_party(role="host", secret=a.vpn.party_secret.hex(), port=port, external_ip=ext_ip)
            a.note("Internet party created. Share the code with your friends.")
            return self._json({"ok": True, "code": result, "party_id": a.vpn.party_id})
        if path == "/api/vpn/join_party":
            code = str(b.get("code", "")).strip()
            if not code and b.get("again"):
                code = str((handoff.last_party(a.cfg.data_dir) or {}).get("code", ""))
            if not code:
                return self._json({"ok": False, "error": "Enter a party code."})
            result = a.vpn.join_party(code)
            if result != "ok":
                return self._json({"ok": False, "error": result})
            a.start_party_auth()          # sign our requests; poll the VPN for the host and the others
            a.remember_party(role="joined", code=code)
            a.note("Joined internet party. Connecting to other players...")
            return self._json({"ok": True})
        if path == "/api/vpn/leave_party":
            a.vpn.stop()
            a.auth = None
            from . import peers as _peers
            _peers.clear_auth()
            a.discovery.clear_vpn_peers()
            handoff.clear_last_party(a.cfg.data_dir)   # left on purpose: no "Rejoin" offer
            a.note("Left the internet party.")
            return self._json({"ok": True})
        if path == "/api/vpn/kick":
            pid = str(b.get("peer_id", ""))
            if not pid:
                return self._json({"ok": False, "error": "No peer_id"})
            if a.auth:
                a.auth.kick_peer(pid)
                a.note(f"Kicked player {pid}")
            return self._json({"ok": True})
        if path == "/api/vpn/audit":
            if a.auth:
                return self._json({"entries": a.auth.audit.recent(100)})
            return self._json({"entries": []})
        if path == "/api/links/probe":
            peers = a.discovery.list()
            results = {}
            for p in peers:
                ip = best_ip(p, force=True)
                results[p["id"]] = {"name": p["name"], "best_ip": ip, "probes": probe_peer(p)}
            return self._json({"ok": True, "results": results})
        if path == "/api/app/update":
            r = a.updater.start(str(b.get("peer_id", "")))
            return self._json({"ok": r == "ok", "error": "" if r == "ok" else r})
        if path == "/api/app/restart":
            if a.updater.status.get("state") != "restart_needed":
                return self._json({"ok": False, "error": "No update is waiting for a restart."})
            a.updater.status = dict(a.updater.status, state="restarting", msg="Restarting…")
            update.restart(a)
            return self._json({"ok": True})
        if path == "/api/defender/exclude":
            folders = a.cfg["folders"]
            if not folders:
                return self._json({"ok": False, "error": "No game folders configured."})
            threading.Thread(target=exclude_folders, args=(a.cfg, folders, a.note), daemon=True).start()
            return self._json({"ok": True})
        return self._json({"error": "unknown"}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # HTTPServer turns on SO_REUSEADDR, which on Windows lets a second launcher bind the same port silently: old and
    # new copies then share 47801 and answer for each other. Take the port exclusively so a second copy fails.
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # Downloaders keep connections open and hang up when done (or mid-stream on a retry); that isn't an error
        # worth a traceback in the console.
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(app: App, port: int) -> ThreadingHTTPServer:
    httpd = Server(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
