"""Tests for lanparty\\handoff.py: an internet party survives an update restart, and the last party is remembered.
A child Python process stands in for n2n's edge/supernode (the real ones need admin and a TAP adapter).
Run: python tests\\test_handoff.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import handoff, peers, security, update, vpn  # noqa: E402
from lanparty.config import Config  # noqa: E402
from lanparty.discovery import Discovery  # noqa: E402
from lanparty.library import Library  # noqa: E402
from lanparty.netwrap import NetWrap  # noqa: E402
from lanparty.peers import PeerSync  # noqa: E402
from lanparty.server import App  # noqa: E402
from lanparty.transfer import Downloads  # noqa: E402
from lanparty.vote import Votes  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


def make_app(data_dir: str) -> App:
    cfg = Config(data_dir)
    lib = Library(cfg)
    disc = Discovery(cfg, 47801)
    votes = Votes(cfg)
    sync = PeerSync(cfg, disc, lib, votes)
    return App(cfg, lib, disc, sync, votes, Downloads(cfg, disc, lib, sync), NetWrap(cfg))


def dummy() -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    vpn._kill_on_exit(p)          # like vpn._spawn does for the real edge
    return p


def signed(key, pid, party, nonce=None):
    ts, n = security.make_timestamp(), nonce or security.make_nonce()
    return {"x-party-id": party, "x-timestamp": ts, "x-nonce": n, "x-peer-id": pid,
            "x-signature": security.sign_request(key, "GET", "/peer/library", b"", ts, n)}


PY = {"edge": sys.executable, "supernode": sys.executable}
tmp = tempfile.mkdtemp(prefix="lp_handoff_")
procs = []
try:
    # ---- DPAPI ----
    blob = handoff.protect(b"secret stuff")
    check(blob is not None and b"secret" not in blob, "DPAPI encrypts")
    check(handoff.unprotect(blob) == b"secret stuff", "DPAPI round-trip")
    bad = bytearray(blob)
    bad[len(bad) // 2] ^= 0xFF
    check(handoff.unprotect(bytes(bad)) is None, "changed blob is refused")
    check(handoff.unprotect(b"not dpapi") is None, "garbage is refused")

    # ---- hand-over file ----
    dd = os.path.join(tmp, "files")
    os.makedirs(dd)
    check(handoff.take_handoff(dd) is None, "no hand-over: None")
    handoff.save_handoff(dd, {"x": 1})
    with open(os.path.join(dd, handoff.HANDOFF_FILE), "rb") as fh:
        check(b'"x"' not in fh.read(), "hand-over file is encrypted on disk")
    check((handoff.take_handoff(dd) or {}).get("x") == 1, "hand-over read back")
    check(not handoff.handoff_waiting(dd) and handoff.take_handoff(dd) is None, "hand-over is used once")
    handoff.save_handoff(dd, {"x": 2})
    ttl, handoff.HANDOFF_TTL = handoff.HANDOFF_TTL, 0.0
    time.sleep(0.05)
    check(handoff.take_handoff(dd) is None and not handoff.handoff_waiting(dd), "expired hand-over refused, deleted")
    handoff.HANDOFF_TTL = ttl

    # ---- last party ----
    check(handoff.last_party(dd) is None, "no last party")
    handoff.save_last_party(dd, {"role": "joined", "code": "ABCD-EFGH"})
    check((handoff.last_party(dd) or {}).get("code") == "ABCD-EFGH", "last party stored")
    check((handoff.last_party(dd) or {}).get("code") == "ABCD-EFGH", "last party kept after reading (until Leave)")
    ttl, handoff.LAST_PARTY_TTL = handoff.LAST_PARTY_TTL, 0.0
    time.sleep(0.05)
    check(handoff.last_party(dd) is None, "last party expires (24 h)")
    handoff.LAST_PARTY_TTL = ttl
    handoff.save_last_party(dd, {"role": "host", "secret": "00" * 32})
    handoff.clear_last_party(dd)
    check(handoff.last_party(dd) is None, "Leave clears the last party")
    with open(os.path.join(dd, "settings.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    handoff.save_last_party(dd, {"role": "host", "secret": "ab" * 32})
    with open(os.path.join(dd, "settings.json"), encoding="utf-8") as fh:
        check("ab" * 32 not in fh.read(), "party secret isn't in settings.json")

    # ---- process identity / adoption ----
    p = dummy()
    procs.append(p)
    ident = handoff.process_identity(p.pid)
    check(ident.get("pid") == p.pid and ident.get("exe", "").lower().endswith("python.exe"), "process identity")
    check(handoff.adopt(ident, r"C:\nowhere\edge.exe") is None, "another exe isn't adopted")
    check(handoff.adopt(dict(ident, started=ident["started"] + 1), sys.executable) is None,
          "a reused pid (other start time) isn't adopted")
    check(handoff.adopt({"pid": 4, "exe": "x", "started": 0}, sys.executable) is None, "a foreign pid isn't adopted")

    # ---- end to end: party, update restart, new launcher takes it over ----
    data = os.path.join(tmp, "data")
    old = make_app(data)
    secret = os.urandom(32)
    v = old.vpn
    v.state, v.my_ip, v.party_secret = "joined", "10.128.0.77", secret
    v.party_id, v.host_ip, v.supernode_port, v.adapter = vpn.party_id_for(secret), "203.0.113.9", 7654, "LanPartyVPN"
    v._edge = dummy()
    procs.append(v._edge)
    edge_pid = v._edge.pid
    old.start_party_auth()
    pid_id = v.party_id
    eve_key = security.derive_player_key(secret, "eve")
    old.auth.verify_request("10.128.0.5", "GET", "/peer/library", b"", signed(eve_key, "eve", pid_id))
    old.auth.kick_peer("eve")
    bob_key = security.derive_player_key(secret, "bob")
    used = security.make_nonce()
    h_used = signed(bob_key, "bob", pid_id, used)
    ok, _, _ = old.auth.verify_request("10.128.0.6", "GET", "/peer/library", b"", h_used)
    check(ok, "bob is a member before the update")
    for _ in range(security.BAD_SIG_LIMIT):
        old.auth.verify_request("10.128.0.66", "GET", "/peer/library", b"", {})
    old.party = {"game_id": "g", "title": "Game", "host_id": "h", "host_name": "H", "host_ip": "10.128.0.1"}

    spawned = []
    real_popen, real_timer, real_stop, real_restore = update.subprocess.Popen, update.threading.Timer, v.stop, \
        vpn.restore_game_network
    stopped, restored = [], []
    update.subprocess.Popen = lambda *a, **k: spawned.append(a)
    update.threading.Timer = lambda *a, **k: type("T", (), {"start": lambda self: None})()
    v.stop = lambda: stopped.append(1)
    vpn.restore_game_network = lambda *a, **k: restored.append(1) or True
    try:
        update.restart(old)
    finally:
        update.subprocess.Popen, update.threading.Timer, v.stop = real_popen, real_timer, real_stop
        vpn.restore_game_network = real_restore
    check(spawned and not stopped and not restored, "update restart keeps the party: VPN not stopped, settings kept")
    check(handoff.handoff_waiting(data), "hand-over written for the new launcher")
    check(v._edge.poll() is None, "edge still running")

    peers.clear_auth()                    # the new launcher is a fresh process: no signing yet
    new = make_app(data)
    check(new.resume_party(PY), "new launcher takes the party over")
    nv = new.vpn
    check(nv.state == "joined" and nv.my_ip == "10.128.0.77" and nv.party_id == pid_id, "same VPN IP and party")
    check(nv._edge is not None and nv._edge.pid == edge_pid and nv._edge.poll() is None, "same edge process adopted")
    check(peers.current_party() == pid_id and peers._sign_headers("GET", "/peer/x", b""), "signing restored")
    check(("10.128.0.1", 47801) in [tuple(x) for x in new.discovery._vpn_peers], "polls the host again")
    check(new.party and new.party.get("title") == "Game", "game party info kept")
    check(not handoff.handoff_waiting(data), "hand-over deleted once used")
    ok, _, r = new.auth.verify_request("10.128.0.6", "GET", "/peer/library", b"", h_used)
    check(not ok and r == "replayed nonce", f"a request from before the update can't be replayed ({r})")
    ok, _, r = new.auth.verify_request("10.128.0.5", "GET", "/peer/library", b"", signed(eve_key, "eve", pid_id))
    check(not ok and r == "kicked", f"a kicked player stays kicked ({r})")
    check(new.auth.limiter.is_blocked("10.128.0.66"), "a blocked IP stays blocked")
    ok, _, _ = new.auth.verify_request("10.128.0.6", "GET", "/peer/library", b"", signed(bob_key, "bob", pid_id))
    check(ok and any(m["id"] == "bob" for m in new.auth.members()), "members keep working")
    check(new.vpn.status()["edge"], "status shows the edge running")
    new.vpn._kill_edge()
    time.sleep(0.3)
    check(p.poll() is None and procs[1].poll() is not None, "leaving stops the adopted edge")
    peers.clear_auth()

    # ---- hand-overs that can't be used: normal cleanup ----
    def handed(state="joined", procs_=None, secret_=None):
        s = secret_ or os.urandom(32)
        handoff.save_handoff(data, {"state": state, "secret": s.hex(), "party_id": vpn.party_id_for(s),
                                    "my_ip": "10.128.0.9", "procs": procs_ or {}})

    q = dummy()
    procs.append(q)
    handed(procs_={"edge": handoff.process_identity(q.pid)})
    a3 = make_app(data)
    check(not a3.resume_party({"edge": r"C:\nowhere\edge.exe", "supernode": r"C:\nowhere\supernode.exe"}),
          "wrong exe: not taken over")
    check(a3.vpn.state == "off" and a3.auth is None and q.poll() is None, "...and a process that isn't ours is left alone")
    handed(procs_={"edge": dict(handoff.process_identity(q.pid), pid=999999)})
    check(not make_app(data).resume_party(PY), "dead/wrong pid: not taken over")
    handed(state="host", procs_={"edge": handoff.process_identity(q.pid)})
    a5 = make_app(data)
    check(not a5.resume_party(PY), "host without its supernode: not taken over")
    time.sleep(0.3)
    check(q.poll() is not None, "...and the leftover edge (ours) is stopped")
    s = os.urandom(32)
    handoff.save_handoff(data, {"state": "joined", "secret": s.hex(), "party_id": "wrong", "procs": {}})
    check(not make_app(data).resume_party(PY), "party id doesn't match the secret: refused")
    with open(os.path.join(data, handoff.HANDOFF_FILE), "wb") as fh:
        fh.write(b"tampered")
    check(not make_app(data).resume_party(PY) and not handoff.handoff_waiting(data), "tampered file: refused, deleted")
    check(not make_app(data).resume_party(PY), "no file: normal start")

    # an update restart outside a party, or when the hand-over can't be written, behaves as before
    a6 = make_app(data)
    a6.vpn.state = "joined"           # active but no edge process: can't hand over
    a6.auth = security.PartyAuth("x", os.urandom(32))
    stopped = []
    update.subprocess.Popen = lambda *a, **k: None
    update.threading.Timer = lambda *a, **k: type("T", (), {"start": lambda self: None})()
    a6.vpn.stop = lambda: stopped.append(1)
    vpn.restore_game_network = lambda *a, **k: True
    try:
        update.restart(a6)
    finally:
        update.subprocess.Popen, update.threading.Timer = real_popen, real_timer
        vpn.restore_game_network = real_restore
    check(stopped and not handoff.handoff_waiting(data), "no edge to hand over: party left as before")

    # ---- for real: a launcher process exits; its n2n child dies, unless it was released for a hand-over ----
    probe = ("import subprocess, sys, time; sys.path.insert(0, sys.argv[1]); from lanparty import vpn\n"
             "d = subprocess.DEVNULL\n"     # like the real edge (output to a log file), not our captured pipe
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=d, stdout=d, stderr=d)\n"
             "vpn._kill_on_exit(p)\n"
             "if sys.argv[2] == '1': assert vpn.release_n2n_for_handoff()\n"
             "print(p.pid, flush=True)\n")
    for release in ("0", "1"):
        out = subprocess.run([sys.executable, "-c", probe, ROOT, release], capture_output=True, text=True,
                             timeout=60).stdout.strip()
        time.sleep(0.5)
        child = int(out.splitlines()[-1]) if out else 0
        alive = bool(handoff.process_identity(child)) if child else False
        if release == "1":
            check(alive, "released n2n outlives the old launcher")
            if alive:
                handoff.stop_if_ours(handoff.process_identity(child), sys.executable)
        else:
            check(child and not alive, "without a hand-over, n2n still dies with the launcher")

    # ---- remember / rejoin data for the UI ----
    a7 = make_app(data)
    a7.remember_party(role="joined", code="WXYZ-1234")
    check(a7.last_party_summary() == {"role": "joined", "at": a7.last_party_summary()["at"], "code": "WXYZ-1234"},
          "Rejoin offer carries the code")
    a7.remember_party(role="host", secret="cd" * 32, port=7654, external_ip="")
    sm = a7.last_party_summary()
    check(sm.get("role") == "host" and "secret" not in sm, "Host-again offer doesn't show the secret")
    check("last_party" in a7.state() and a7.state()["last_party"].get("role") == "host", "state() offers it")
finally:
    for pr in procs:
        try:
            pr.kill()
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
