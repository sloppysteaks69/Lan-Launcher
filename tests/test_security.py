"""Unit tests for security module: signing, verification, nonces, rate limiting, party codes.
Run: python tests\\test_security.py"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import security  # noqa: E402
from lanparty.vpn import encode_party_code, decode_party_code  # noqa: E402
from lanparty.vpn import decode_party_code_full as vpn_full  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


# ---- signing and verification ----
secret = security.make_party_secret()
party_id = security.make_party_id()
auth = security.PartyAuth(party_id, secret)
peer_id = "alice123"
key = auth.add_peer(peer_id, "Alice")

# sign a request and verify it
ts = security.make_timestamp()
nonce = security.make_nonce()
body = b'{"test": true}'
sig = security.sign_request(key, "POST", "/peer/library", body, ts, nonce)

headers = {"x-party-id": party_id, "x-timestamp": ts, "x-nonce": nonce,
           "x-signature": sig, "x-peer-id": peer_id}
ok, pid, reason = auth.verify_request("1.2.3.4", "POST", "/peer/library", body, headers)
check(ok, "valid signature accepted")
check(pid == peer_id, "correct peer_id returned")

# wrong signature (fresh nonce so it doesn't trip the replay check)
bad_nonce = security.make_nonce()
bad_ts = security.make_timestamp()
bad_headers = {"x-party-id": party_id, "x-timestamp": bad_ts, "x-nonce": bad_nonce,
               "x-signature": "0" * 64, "x-peer-id": peer_id}
ok2, _, reason2 = auth.verify_request("9.9.9.9", "POST", "/peer/library", body, bad_headers)
check(not ok2, "bad signature rejected")
check(reason2 == "bad signature", f"reason is 'bad signature': {reason2}")

# replayed nonce
nonce2 = security.make_nonce()
ts2 = security.make_timestamp()
sig2 = security.sign_request(key, "GET", "/peer/library", b"", ts2, nonce2)
h2 = {"x-party-id": party_id, "x-timestamp": ts2, "x-nonce": nonce2,
      "x-signature": sig2, "x-peer-id": peer_id}
ok3, _, _ = auth.verify_request("2.2.2.2", "GET", "/peer/library", b"", h2)
check(ok3, "first use of nonce accepted")
# replay the same nonce
ok4, _, reason4 = auth.verify_request("2.2.2.2", "GET", "/peer/library", b"", h2)
check(not ok4, "replayed nonce rejected")
check(reason4 == "replayed nonce", f"reason is 'replayed nonce': {reason4}")

# expired timestamp
old_ts = str(int(time.time()) - 120)
nonce5 = security.make_nonce()
sig5 = security.sign_request(key, "GET", "/peer/hello", b"", old_ts, nonce5)
h5 = {"x-party-id": party_id, "x-timestamp": old_ts, "x-nonce": nonce5,
      "x-signature": sig5, "x-peer-id": peer_id}
ok5, _, reason5 = auth.verify_request("3.3.3.3", "GET", "/peer/hello", b"", h5)
check(not ok5, "expired timestamp rejected")

# missing headers
ok6, _, reason6 = auth.verify_request("4.4.4.4", "GET", "/peer/library", b"", {})
check(not ok6, "missing headers rejected")

# kicked peer
auth.kick_peer(peer_id)
nonce7 = security.make_nonce()
ts7 = security.make_timestamp()
sig7 = security.sign_request(key, "GET", "/peer/library", b"", ts7, nonce7)
h7 = {"x-party-id": party_id, "x-timestamp": ts7, "x-nonce": nonce7,
      "x-signature": sig7, "x-peer-id": peer_id}
ok7, _, reason7 = auth.verify_request("5.5.5.5", "GET", "/peer/library", b"", h7)
check(not ok7, "kicked peer rejected")
check(reason7 == "kicked", f"reason is 'kicked': {reason7}")

# auto-register: a new peer with a valid signature (derived from party secret) is accepted
auth2 = security.PartyAuth("p2", secret)
new_id = "bob456"
new_key = security.derive_player_key(secret, new_id)
nonce8 = security.make_nonce()
ts8 = security.make_timestamp()
sig8 = security.sign_request(new_key, "GET", "/peer/library", b"", ts8, nonce8)
h8 = {"x-party-id": "p2", "x-timestamp": ts8, "x-nonce": nonce8,
      "x-signature": sig8, "x-peer-id": new_id}
ok8, pid8, _ = auth2.verify_request("6.6.6.6", "GET", "/peer/library", b"", h8)
check(ok8, "auto-registered new peer")
check(pid8 == new_id, "auto-registered with correct id")
check(any(m["id"] == new_id for m in auth2.members()), "new peer in members list")

auth2.learn_names({new_id: "Bob"})
check([m["name"] for m in auth2.members() if m["id"] == new_id] == ["Bob"], "auto-registered member gets its name")

# ---- rate limiter ----
limiter = security.RateLimiter()
for _ in range(59):
    limiter.check("10.0.0.1")
r = limiter.check("10.0.0.1")
check(r == "", "59 requests allowed")
r = limiter.check("10.0.0.1")
check(r == "rate-limited", "60th request rate-limited")

# bad sig limit
limiter2 = security.RateLimiter()
for _ in range(9):
    limiter2.bad_sig("10.0.0.2")
blocked = limiter2.bad_sig("10.0.0.2")
check(blocked, "10 bad sigs blocks the IP")

# ---- party code ----
code = encode_party_code("203.0.113.42", 7654, os.urandom(32))
check("-" in code, "party code has dashes")
ip, port, sec = decode_party_code(code)
check(ip == "203.0.113.42", f"decoded IP: {ip}")
check(port == 7654, f"decoded port: {port}")
check(len(sec) == 32, f"decoded secret length: {len(sec)}")

# round-trip with various IPs
for test_ip in ("192.168.1.1", "10.0.0.1", "255.255.255.255", "1.2.3.4"):
    s = os.urandom(32)
    c = encode_party_code(test_ip, 12345, s)
    i, p, ss = decode_party_code(c)
    check(i == test_ip and p == 12345 and ss == s, f"round-trip {test_ip}")

# ---- host LAN IPs in the code, for joiners in the host's room ----
s = os.urandom(32)
old = encode_party_code("203.0.113.42", 7654, s)
check(len(old.replace("-", "")) == 61, "a code without LAN IPs is the 1.1.0 code")
check(decode_party_code(old) == ("203.0.113.42", 7654, s), "old codes still decode")
check(vpn_full(old)[3] == [], "old code: no LAN IPs")
new = encode_party_code("203.0.113.42", 7654, s, ["192.168.1.20"])
check(len(new.replace("-", "")) - 61 <= 7, f"one LAN IP adds at most 7 characters ({new})")
check(vpn_full(new) == ("203.0.113.42", 7654, s, ["192.168.1.20"]), "LAN IP round-trips")
check(decode_party_code(new) == ("203.0.113.42", 7654, s), "3-value decode still works on the new code")
two = encode_party_code("203.0.113.42", 7654, s, ["192.168.1.20", "10.0.0.5", "172.16.0.9"])
check(vpn_full(two)[3] == ["192.168.1.20", "10.0.0.5"], "at most 2 LAN IPs")
check(vpn_full(new.lower().replace("-", " "))[3] == ["192.168.1.20"], "lower case and spaces accepted")
try:
    vpn_full(new[:-3])
    check(False, "truncated code should raise")
except ValueError:
    check(True, "truncated code raises ValueError")

from lanparty import vpn as _vpn  # noqa: E402
check(not any(ip.startswith("10.128.0.") or ip.startswith("127.") for ip in _vpn.host_lan_ips()),
      f"host LAN IPs leave out the VPN and loopback ({_vpn.host_lan_ips()})")
check(_vpn.host_lan_ips("203.0.113.42") == _vpn.host_lan_ips(), "host LAN IPs work with an external IP given")
mine = ["10.128.0.34", "192.168.1.30", "192.168.56.1"]
check(_vpn.supernode_order("47.1.2.3", ["192.168.1.20"], mine, lambda ip: True) == ["192.168.1.20", "47.1.2.3"],
      "host confirmed on our LAN: its LAN address first, public second")
check(_vpn.supernode_order("47.1.2.3", ["192.168.1.20"], mine, lambda ip: False) == ["47.1.2.3", "192.168.1.20"],
      "same subnet but no answer: public first, LAN as fallback")
check(_vpn.supernode_order("47.1.2.3", ["192.168.7.7"], mine, lambda ip: True) == ["47.1.2.3"],
      "host on another subnet: public only, no probe")
check(_vpn.supernode_order("47.1.2.3", [], mine, lambda ip: True) == ["47.1.2.3"], "old code: public only")
check(_vpn.supernode_order("47.1.2.3", ["8.8.8.8", "10.128.0.1"], ["8.8.8.9", "10.128.0.2"], lambda ip: True)
      == ["47.1.2.3"], "public or VPN addresses in the code are ignored")
check(not _vpn.probe_lan_host("127.0.0.1", "nope", timeout=0.5), "probe refuses a launcher that isn't the host")

# bad code
try:
    decode_party_code("ZZZZ")
    check(False, "bad code should raise")
except (ValueError, Exception):
    check(True, "bad code raises ValueError")

# ---- member traffic isn't throttled at the stranger limit ----
auth3 = security.PartyAuth("p3", secret)
mkey = security.derive_player_key(secret, "carol")
member_ok = 0
for i in range(300):     # ~4 minutes of library/vote sync, or a 300-file download
    ts9, n9 = security.make_timestamp(), security.make_nonce()
    s9 = security.sign_request(mkey, "GET", f"/peer/file/g{i}", b"", ts9, n9)
    ok9, _, _ = auth3.verify_request("7.7.7.7", "GET", f"/peer/file/g{i}",  b"",
                                     {"x-party-id": "p3", "x-timestamp": ts9, "x-nonce": n9,
                                      "x-signature": s9, "x-peer-id": "carol"})
    member_ok += ok9
check(member_ok == 300, f"300 signed member requests in a burst accepted ({member_ok})")

# ---- outgoing signing matches what the server verifies (path without the query) ----
from lanparty import peers  # noqa: E402
auth4 = security.PartyAuth("p4", secret)
peers.set_auth("p4", security.derive_player_key(secret, "dave"), "dave")
hh = {k.lower(): v for k, v in peers._sign_headers("GET", "/peer/file/abc?p=data%2Fx.pak", b"").items()}
ok10, _, r10 = auth4.verify_request("8.8.8.8", "GET", "/peer/file/abc", b"", hh)
check(ok10, f"signed file fetch with a query verifies on the server path ({r10})")
peers.clear_auth()
check(peers._sign_headers("GET", "/peer/x", b"") == {}, "no signing outside a party")

# ---- background sync doesn't knock on a party PC it can't sign for (it would block our IP after 10) ----
check(peers.current_party() == "", "no party advertised outside a party")
check(not peers.can_sync({"id": "h", "party": "p4"}), "outside a party: skip a peer whose hello says it's in one")
check(peers.can_sync({"id": "h", "party": ""}) and peers.can_sync({"id": "h"}), "sync with non-party / old peers")


class _E403(OSError):
    code = 403


peers.note_refused("old", OSError("timed out"))
check(peers.can_sync({"id": "old"}), "a timeout isn't a refusal")
peers.note_refused("old", _E403())
check(not peers.can_sync({"id": "old"}), "a 403 pauses sync with that peer")
peers._refused["old"] = time.time() - peers.REFUSED_RETRY - 1
check(peers.can_sync({"id": "old"}), "retried after REFUSED_RETRY")
peers.note_refused("old", _E403())
peers.set_auth("p4", security.derive_player_key(secret, "dave"), "dave")
check(peers.can_sync({"id": "old"}), "joining a party clears the refusals")
check(peers.current_party() == "p4", "party id advertised in a party")
check(peers.can_sync({"id": "h", "party": "p4"}), "same party: sync")
check(not peers.can_sync({"id": "h", "party": "other"}), "different party: skip")
peers.clear_auth()
blocked_ip = security.PartyAuth("p5", secret)
sent = 0
for _ in range(100):          # ~75 s of the 1.5 s sync loop before the joiner enters the code
    if peers.can_sync({"id": "host"}):
        sent += 1
        blocked_ip.verify_request("192.168.1.30", "GET", "/peer/library", b"", {})
        peers.note_refused("host", _E403())
check(sent == 1, f"an unsigned joiner knocks once, not every loop ({sent})")
check(not blocked_ip.limiter.is_blocked("192.168.1.30"), "so the host doesn't block the joiner's IP")
peers.forget_refusals()

check(peers.pick_ip({"10.128.0.1": 1.2, "192.168.1.20": 2.9}) == "192.168.1.20",
      "same room: the LAN address wins over a slightly faster-looking VPN address")
check(peers.pick_ip({"10.128.0.1": 40.0, "192.168.1.20": 0.6}) == "192.168.1.20", "LAN clearly faster: LAN")
check(peers.pick_ip({"10.128.0.1": 45.0}) == "10.128.0.1", "remote player: only the VPN answers")
check(peers.pick_ip({"10.128.0.1": 1.0, "172.17.0.5": 30.0}) == "10.128.0.1", "a slow odd adapter doesn't beat the VPN")

from lanparty.discovery import Discovery  # noqa: E402
d = Discovery({"peer_id": "me", "name": "Me"}, 47801)
d.party = lambda: "p9"
check(d.hello().get("party") == "p9", "hello carries the party id")
d.seen({"app": "lanparty", "id": "x", "port": 47801, "party": "p9"}, "1.2.3.4")
check(d.get("x").get("party") == "p9", "peer's party id kept from its hello")
d.seen({"app": "lanparty", "id": "y", "port": 47801}, "1.2.3.5")
check(d.get("y").get("party") == "", "older hello without party")

# ---- n2n helpers ----
from lanparty import vpn  # noqa: E402
s = os.urandom(32)
check(vpn.party_id_for(s) == vpn.party_id_for(bytes(s)), "host and joiner derive the same party id")
check(len(vpn.n2n_key_for(s)) == 32 and vpn.n2n_key_for(s).isascii(), "n2n6 key is 32 ASCII chars")
check(vpn.n2n_key_for(s) != vpn.n2n_key_for(os.urandom(32)), "n2n key depends on the secret")
v = vpn.VPN(os.path.join(os.environ.get("TEMP", "."), "lp_test_vpn"))
v.party_code = "ABCD-EFGH"
check(v.status().get("code") == "ABCD-EFGH", "party code is in the VPN status")
v.stop()
check(v.status().get("code") == "", "party code cleared on leave")

# ---- audit log ----
log = security.AuditLog(capacity=5)
for i in range(7):
    log.add(f"10.0.0.{i}", f"p{i}", "GET", "/peer/hello", True)
check(len(log.recent(100)) == 5, "audit log capped at capacity")
check(log.recent(2)[-1]["ip"] == "10.0.0.6", "audit log keeps newest")

# ---- summary ----
if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
