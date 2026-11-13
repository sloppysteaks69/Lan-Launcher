"""Unit tests for the VPN game network step (metric + firewall rule) and discovery's VPN broadcast.
Nothing real is changed: PowerShell and netsh are replaced by fakes. Run: python tests\\test_gamenet.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import vpn  # noqa: E402
from lanparty.discovery import Discovery  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


class Fake:
    """Stands in for vpn._ps and subprocess.run; records every command."""

    def __init__(self, metric="25,Enabled", admin=True, rule_exists=False):
        self.ps, self.netsh = [], []
        self.metric, self.admin, self.rule_exists = metric, admin, rule_exists

    vpn_alias = "LanPartyVPN"
    blocks = None   # rule name -> {"display", "program", "enabled"}

    def run_ps(self, cmd, timeout=30):
        self.ps.append(cmd)
        blocks = self.blocks if self.blocks is not None else {}
        if cmd.startswith("$exes = @("):
            found = [{"name": n, "display": r["display"], "program": r["program"]} for n, r in blocks.items()
                     if r["enabled"] and ("'" + r["program"] + "'").lower() in cmd.lower()]
            return subprocess.CompletedProcess([], 0, json.dumps(found) if found else "", "")
        for verb, state in (("Disable-NetFirewallRule -Name '", False), ("Enable-NetFirewallRule -Name '", True)):
            if cmd.startswith(verb):
                if not self.admin:
                    return subprocess.CompletedProcess([], 1, "", "Access is denied.")
                name = cmd[len(verb):].split("'")[0]
                if name not in blocks:
                    return subprocess.CompletedProcess([], 1, "", "No MSFT_NetFirewallRule objects found")
                blocks[name]["enabled"] = state
                return subprocess.CompletedProcess([], 0, "ok\n", "")
        if cmd.startswith("Get-NetIPAddress -IPAddress"):
            return subprocess.CompletedProcess([], 0, json.dumps({"InterfaceIndex": 12,
                                                                  "InterfaceAlias": self.vpn_alias}), "")
        if cmd.startswith("$p = Get-NetConnectionProfile"):
            return subprocess.CompletedProcess([], 0, "ok\n", "")
        if cmd.startswith("$i = Get-NetIPInterface"):
            return subprocess.CompletedProcess([], 0, self.metric + "\n", "")
        if cmd.startswith("Set-NetIPInterface"):
            if not self.admin:
                return subprocess.CompletedProcess([], 1, "", "Access denied")
            return subprocess.CompletedProcess([], 0, "ok\n", "")
        return subprocess.CompletedProcess([], 0, "", "")

    def run(self, cmd, **kw):
        self.netsh.append(cmd[1:])
        if not self.admin:
            return subprocess.CompletedProcess(cmd, 1, "The requested operation requires elevation.", "")
        if "delete" in cmd:
            had, self.rule_exists = self.rule_exists, False
            return subprocess.CompletedProcess(cmd, 0 if had else 1,
                                               "Deleted 1 rule(s)." if had else "No rules match the specified criteria.", "")
        if "add" in cmd:
            self.rule_exists = True
        return subprocess.CompletedProcess(cmd, 0, "Ok.", "")


def install(fake):
    vpn._ps = fake.run_ps
    vpn.subprocess.run = fake.run


real_ps, real_run = vpn._ps, subprocess.run
tmp = tempfile.mkdtemp(prefix="lanparty_gamenet_")
try:
    backup = os.path.join(tmp, "n2n", "metric_backup.json")
    rule = "name=" + vpn.GAMENET_RULE

    # ---- apply
    f = Fake(metric="25,Enabled", rule_exists=True)
    install(f)
    log = []
    ok = vpn.apply_game_network(tmp, log)
    check(ok, "apply returns True as admin")
    check(os.path.isfile(backup), "metric_backup.json written")
    saved = json.load(open(backup, encoding="utf-8"))
    check(saved.get("metric") == 25 and saved.get("automatic") is True, "backup holds metric 25, automatic")
    check(any("Set-NetIPInterface -InterfaceAlias 'LanPartyVPN' -AddressFamily IPv4 -InterfaceMetric 1" in c
              for c in f.ps), "metric set to 1 on LanPartyVPN")
    adds = [c for c in f.netsh if "add" in c]
    dels = [c for c in f.netsh if "delete" in c]
    check(len(adds) == 1 and adds[0] == ["advfirewall", "firewall", "add", "rule", rule, "dir=in", "action=allow",
                                         "remoteip=10.128.0.0/24", "profile=any"],
          "one allow rule from 10.128.0.0/24, no program/protocol/port limit")
    check(f.netsh.index(dels[0]) < f.netsh.index(adds[0]), "old rule deleted before adding")
    check(not any(a.startswith(("program=", "protocol=", "localport=")) for a in adds[0]), "rule has no limits")
    check(any("metric set to 1" in m for m in log) and any("Firewall: allowed all games" in m for m in log),
          "both steps logged")

    # ---- apply again keeps the first backup (the pre-party metric)
    f.metric = "1,Disabled"
    vpn.apply_game_network(tmp, [])
    check(json.load(open(backup, encoding="utf-8")).get("metric") == 25, "second apply keeps the original backup")

    # ---- restore
    log = []
    ok = vpn.restore_game_network(tmp, log)
    check(ok, "restore returns True")
    check(any("-AutomaticMetric Enabled" in c for c in f.ps), "automatic metric restored")
    check(f.netsh[-1] == ["advfirewall", "firewall", "delete", "rule", rule], "rule deleted on restore")
    check(not f.rule_exists, "rule gone")
    check(not os.path.exists(backup), "backup deleted after restore")
    n = len(f.ps)
    check(vpn.restore_game_network(tmp, []) and len(f.ps) == n, "restore with no backup does nothing")

    # ---- fixed metric restored as a number
    f = Fake(metric="40,Disabled")
    install(f)
    vpn.apply_game_network(tmp, [])
    vpn.restore_game_network(tmp, [])
    check(any("-InterfaceMetric 40" in c for c in f.ps if c.startswith("Set-")), "manual metric 40 restored")

    # ---- not admin: carries on, logs, keeps the backup for later
    f = Fake(admin=False)
    install(f)
    log = []
    ok = vpn.apply_game_network(tmp, log)
    check(not ok and any("admin needed" in m for m in log), "non-admin apply fails softly and logs")
    log = []
    check(not vpn.restore_game_network(tmp, log) and os.path.isfile(backup), "non-admin restore keeps the backup")

    # ---- crash recovery on start
    f = Fake(admin=True, rule_exists=True)
    install(f)
    log = []
    vpn.recover_game_network(tmp, log)
    check(any("left by the last session" in m for m in log), "recovery logged")
    check(not os.path.exists(backup) and not f.rule_exists, "recovery restored and cleaned up")
    check(any("-AutomaticMetric Enabled" in c for c in f.ps), "recovery restored the metric")
    f = Fake()
    install(f)
    vpn.recover_game_network(tmp, [])
    check(not f.ps and not f.netsh, "recovery with nothing left does nothing")

    # ---- VPN.stop() restores
    f = Fake()
    install(f)
    vpn.apply_game_network(tmp, [])
    v = vpn.VPN(tmp)
    v.stop()
    check(not os.path.exists(backup) and any("set back" in m for m in v.log), "VPN.stop() restores")

    # ---- n2n opened another TAP adapter (seen on a test PC: 10.128.0.1 on "Local Area Connection")
    lac = "Local Area Connection"
    f = Fake(metric="25,Enabled")
    f.vpn_alias = lac
    install(f)
    ad = vpn.vpn_adapter_for("10.128.0.1", tries=1)
    check(ad == {"alias": lac, "index": 12}, "adapter found from the VPN address")
    check(any("Get-NetIPAddress -IPAddress '10.128.0.1'" in c for c in f.ps), "looked up by IP")
    vpn.apply_game_network(tmp, [], ad)
    saved = json.load(open(backup, encoding="utf-8"))
    check(saved.get("alias") == lac and saved.get("index") == 12, "backup stores the real adapter")
    check(any(c.startswith(f"Set-NetIPInterface -InterfaceAlias '{lac}'") and "-InterfaceMetric 1" in c
              for c in f.ps), "metric 1 goes to Local Area Connection")
    check(not any(c.startswith("Set-NetIPInterface -InterfaceAlias 'LanPartyVPN'") for c in f.ps),
          "LanPartyVPN left alone")
    f.ps.clear()
    vpn.restore_game_network(tmp, [])
    check(any(c.startswith(f"Set-NetIPInterface -InterfaceAlias '{lac}'") and "-AutomaticMetric Enabled" in c
              for c in f.ps), "restore targets Local Area Connection")
    check(not os.path.exists(backup), "backup deleted")

    vpn.apply_game_network(tmp, [], ad)        # crash, then the next start
    f.ps.clear()
    vpn.recover_game_network(tmp, [])
    check(any(c.startswith(f"Set-NetIPInterface -InterfaceAlias '{lac}'") for c in f.ps),
          "crash recovery targets Local Area Connection")

    v = vpn.VPN(tmp)
    v.my_ip = "10.128.0.1"
    f.ps.clear()
    f.netsh.clear()
    v._game_network(1390)
    warn = [m for m in v.log if m.startswith("Warning: n2n is using 'Local Area Connection' instead of LanPartyVPN")]
    check(len(warn) == 1 and "Likely cause:" in warn[0], "warning logged with a likely cause")
    check(any(c.startswith(f"$p = Get-NetConnectionProfile -InterfaceAlias '{lac}'") for c in f.ps),
          "Private profile goes to Local Area Connection")
    check(any(c[:5] == ["interface", "ipv4", "set", "subinterface", lac] for c in f.netsh),
          "MTU goes to Local Area Connection")
    st = v.status()
    check(st["vpn_adapter"] == lac and st["vpn_adapter_ok"] is False and st["game_network"], "status shows the adapter")
    v.stop()
    check(not os.path.exists(backup), "stop restores the other adapter")

    f = Fake()                                 # normal case: no warning
    install(f)
    v = vpn.VPN(tmp)
    v.my_ip = "10.128.0.41"
    v._game_network(1326)
    check(not any(m.startswith("Warning") for m in v.log) and v.status()["vpn_adapter_ok"], "LanPartyVPN: no warning")
    v.stop()

    # ---- game block rules (laptop: two 'Dark Reign 2' Block rules beat every allow rule)
    dr2 = r"C:\Games\Dark Reign 2\dr2.exe"

    def fresh_blocks():
        return {"{T}": {"display": "Dark Reign 2", "program": dr2.lower(), "enabled": True},
                "{U}": {"display": "Dark Reign 2", "program": dr2.lower(), "enabled": True},
                "{X}": {"display": "Other", "program": r"c:\other\x.exe", "enabled": True}}

    f = Fake()
    f.blocks = fresh_blocks()
    install(f)
    vpn.apply_game_network(tmp, [])
    log = []
    n = vpn.disable_game_block_rules(tmp, [dr2], log)
    check(n == 2 and not f.blocks["{T}"]["enabled"] and not f.blocks["{U}"]["enabled"], "both DR2 block rules disabled")
    check(f.blocks["{X}"]["enabled"], "another program's block rule left alone")
    check(not any(c.startswith("Remove-NetFirewallRule") for c in f.ps), "rules disabled, never deleted")
    check(sum(m == f"Disabled block rule 'Dark Reign 2' for {dr2.lower()} during the party." for m in log) == 2,
          "each disabled rule logged")
    saved = json.load(open(backup, encoding="utf-8"))
    check(sorted(r["name"] for r in saved.get("disabled_rules", [])) == ["{T}", "{U}"] and saved.get("metric") == 25,
          "backup records the rules and keeps the metric")
    check(vpn.disable_game_block_rules(tmp, [dr2], []) == 0, "second pass disables nothing new")
    log = []
    check(vpn.restore_game_network(tmp, log), "restore ok")
    check(f.blocks["{T}"]["enabled"] and f.blocks["{U}"]["enabled"], "restore turns both rules back on")
    check(any("-AutomaticMetric Enabled" in c for c in f.ps) and not os.path.exists(backup),
          "restore also puts the metric back and deletes the backup")

    # rules disabled before any metric backup (rules_only), then the party step runs
    f = Fake(metric="30,Disabled")
    f.blocks = fresh_blocks()
    install(f)
    vpn.disable_game_block_rules(tmp, [dr2], [])
    check(json.load(open(backup, encoding="utf-8")).get("rules_only") is True, "rules-only backup written")
    vpn.apply_game_network(tmp, [])
    saved = json.load(open(backup, encoding="utf-8"))
    check(saved.get("metric") == 30 and not saved.get("rules_only") and len(saved.get("disabled_rules", [])) == 2,
          "metric backup added, rules kept")
    vpn.restore_game_network(tmp, [])
    check(f.blocks["{T}"]["enabled"] and any("-InterfaceMetric 30" in c for c in f.ps), "both undone")

    f = Fake()
    f.blocks = fresh_blocks()
    install(f)
    vpn.disable_game_block_rules(tmp, [dr2], [])
    f.ps.clear()
    vpn.restore_game_network(tmp, [])
    check(f.blocks["{U}"]["enabled"] and not any(c.startswith("Set-NetIPInterface") for c in f.ps)
          and not os.path.exists(backup), "rules-only restore leaves the metric alone")

    # crash recovery, a rule deleted meanwhile, and non-admin
    f = Fake(admin=True)
    f.blocks = fresh_blocks()
    install(f)
    vpn.apply_game_network(tmp, [])
    vpn.disable_game_block_rules(tmp, [dr2], [])
    del f.blocks["{U}"]
    f.admin = False
    check(not vpn.restore_game_network(tmp, []) and os.path.exists(backup), "non-admin: backup kept")
    f.admin = True
    log = []
    vpn.recover_game_network(tmp, log)
    check(f.blocks["{T}"]["enabled"] and not os.path.exists(backup), "crash recovery turns the rule back on")
    check(any("is gone" in m for m in log), "a deleted rule doesn't block recovery")

    # the party step and a game started during the party
    f = Fake()
    f.blocks = fresh_blocks()
    install(f)
    v = vpn.VPN(tmp)
    v.my_ip = "10.128.0.41"
    v.game_exes = lambda: [dr2]
    v._game_network(1326)
    check(not f.blocks["{T}"]["enabled"] and v.status()["disabled_block_rules"] == ["Dark Reign 2", "Dark Reign 2"],
          "party start disables the library's block rules")
    f.blocks["{X}"]["program"] = r"c:\games\x\x.exe"
    check(v.open_blocked_for([r"C:\Games\X\x.exe"]) == 0, "not in a party: game start changes nothing")
    v.state = "joined"
    check(v.open_blocked_for([r"C:\Games\X\x.exe"]) == 1 and not f.blocks["{X}"]["enabled"],
          "game started during the party: its block rule disabled")
    v.stop()
    check(all(r["enabled"] for r in f.blocks.values()) and not os.path.exists(backup), "stop turns all three back on")
finally:
    vpn._ps, subprocess.run = real_ps, real_run
    shutil.rmtree(tmp, ignore_errors=True)

# ---- discovery
d = Discovery({"peer_id": "x", "name": "t", "static_peers": []}, 47801)
plan = d.announce_plan(["192.168.56.1", "10.0.0.72"])
check((None, "255.255.255.255") in plan and (None, "192.168.56.255") in plan, "unbound targets kept")
check(("192.168.56.1", "255.255.255.255") in plan and ("10.0.0.72", "255.255.255.255") in plan,
      "a bound socket per local address sends 255.255.255.255")
check(not any(t == "10.128.0.255" for _, t in plan), "no 10.128.0.255 without a VPN")
plan = d.announce_plan(["192.168.56.1", "10.128.0.7"])
check((None, "10.128.0.255") in plan and ("10.128.0.7", "10.128.0.255") in plan, "10.128.0.255 sent with VPN address")
d.add_vpn_peer("10.128.0.7", 47801)
plan = d.announce_plan(["192.168.56.1"])
check((None, "10.128.0.255") in plan, "10.128.0.255 sent while VPN peers are set")

print("ALL_PASS" if not fails else f"FAILED: {len(fails)}")
sys.exit(1 if fails else 0)
