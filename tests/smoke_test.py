"""Two launchers on one PC: library merge, ranked-choice vote sync, and streaming a game from A to B.

Run from the project folder:  python tests\\smoke_test.py"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty.vote import irv  # noqa: E402
from lanparty import launcher, update, VERSION  # noqa: E402
from lanparty.library import load_lan_json  # noqa: E402
from lanparty.netwrap import safe_netsh  # noqa: E402


class FakeCfg(dict):
    def set(self, k, v):
        self[k] = v


def call(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-LanParty": "1"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait(pred, secs=20):
    end = time.time() + secs
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def main() -> None:
    fails = []

    def check(c, what):
        print(("PASS " if c else "FAIL ") + what)
        if not c:
            fails.append(what)

    # IRV unit checks
    r = irv(["a", "b", "c"], [["a", "b"], ["b", "a"], ["c", "b"], ["b"], ["a", "c"]])
    check(r["winner"] in ("a", "b"), f"irv picks a majority winner ({r['winner']})")
    r = irv(["a", "b", "c"], [["c", "b"], ["b"], ["a", "b"], ["c", "b"], ["a", "c"]])
    check(r["winner"] is not None and len(r["rounds"]) >= 2, f"irv runs extra rounds ({r['winner']})")
    check(irv(["x", "y"], [])["winner"] is None, "irv: no ballots, no winner")

    tmp = tempfile.mkdtemp(prefix="lanparty_test_")
    try:
        ga = os.path.join(tmp, "gamesA")
        gb = os.path.join(tmp, "gamesB")
        for d in ("Space Blaster", os.path.join("Space Blaster", "data"), "Warcraft II"):
            os.makedirs(os.path.join(ga, d), exist_ok=True)
        os.makedirs(os.path.join(gb, "Tank Duel"), exist_ok=True)
        shutil.copy(sys.executable, os.path.join(ga, "Space Blaster", "blaster.exe"))
        with open(os.path.join(ga, "Space Blaster", "data", "big.bin"), "wb") as f:
            f.write(os.urandom(5 * 1024 * 1024 + 123))
        with open(os.path.join(ga, "Warcraft II", "WAR2.EXE"), "wb") as f:
            f.write(b"MZ" + b"\0" * 62 + b"IPX network game")
        shutil.copy(sys.executable, os.path.join(gb, "Tank Duel", "tank.exe"))
        with open(os.path.join(ga, "Space Blaster", "lan.json"), "w", encoding="utf-8") as f:
            json.dump({"game": "Space Blaster", "exe": "blaster.exe",
                       "setup": {"script": "setup.bat"},
                       "host": {"command": "exe", "args": ["-server", "-name={player_name}"], "then_join": True},
                       "join": {"command": "blaster.exe", "args": ["{host_ip}:7777", "-name={player_name}"]},
                       "ports": {"udp": "7777,7778-7780", "tcp": "bad;ports"},
                       "firewall": ["netsh advfirewall firewall add rule name=\"SB\" dir=in action=allow protocol=UDP localport=7777",
                                    "netsh advfirewall firewall add rule name=x & del C:\\x"],
                       "players": {"min": 2, "max": 8}}, f)
        with open(os.path.join(ga, "Space Blaster", "setup.bat"), "w") as f:
            f.write("@exit /b 0\r\n")

        # lan.json parsing and command lines (no processes started)
        lan = load_lan_json(os.path.join(ga, "Space Blaster"))
        check(lan.get("ports") == {"udp": "7777,7778-7780"}, f"lan.json: bad port string dropped ({lan.get('ports')})")
        check(lan.get("players") == {"min": 2, "max": 8}, "lan.json: players read")
        g = {"id": "spaceblaster", "path": os.path.join(ga, "Space Blaster"), "exe": "blaster.exe", "lan": lan}
        argv, cwd = launcher.build_argv(g, "join", "192.168.1.50", 'Player "the" Great')
        check(argv[1:] == ["192.168.1.50:7777", "-name=Player the Great"], f"join argv filled in ({argv[1:]})")
        argv, _ = launcher.build_argv(g, "host", "192.168.1.50", "Player")
        check(argv[0].lower().endswith("blaster.exe") and argv[1:] == ["-server", "-name=Player"], "host argv uses exe")
        bad = dict(g, lan={"join": {"command": "..\\..\\Windows\\System32\\calc.exe", "args": []}})
        try:
            launcher.build_argv(bad, "join", "1.2.3.4", "x")
            check(False, "command outside the game folder refused")
        except ValueError:
            check(True, "command outside the game folder refused")
        cfg = FakeCfg(setup_done={})
        check(launcher.setup_needed(g, cfg), "setup needed before it runs")
        cfg["setup_done"] = {"spaceblaster": "setup.bat"}
        check(not launcher.setup_needed(g, cfg), "setup not needed after it ran")
        check(safe_netsh(lan["firewall"][0]) and not safe_netsh(lan["firewall"][1]), "only plain netsh rules allowed")

        procs = []
        # --no-discovery: no LAN broadcast, so Alice and Bob only know each other (and the real launchers on the
        # LAN never see their test poll)
        for name, port, folder, peer in (("Alice", 47811, ga, "127.0.0.1:47812"), ("Bob", 47812, gb, "127.0.0.1:47811")):
            procs.append(subprocess.Popen([sys.executable, "-m", "lanparty", "--port", str(port), "--name", name,
                                           "--data", os.path.join(tmp, "data" + name), "--folder", folder,
                                           "--peer", peer, "--no-browser", "--no-discovery"], cwd=ROOT))
        try:
            check(wait(lambda: [p["name"] for p in call(47811, "state")["peers"]] == ["Bob"]),
                  "Alice only sees Bob (no real launchers from the LAN)")
            check(wait(lambda: any(p["name"] == "Bob" for p in call(47811, "state")["peers"])
                       and any(p["name"] == "Alice" for p in call(47812, "state")["peers"])),
                  "both launchers see each other")
            want = {"spaceblaster", "warcraft2", "tankduel"}
            check(wait(lambda: want <= {g["id"] for g in call(47812, "state")["library"]}),
                  "Bob sees the merged library (Alice's 2 games + his 1)")
            lib = {g["id"]: g for g in call(47812, "state")["library"]}
            check(not lib.get("spaceblaster", {}).get("have", True), "Space Blaster greyed on Bob")
            check(lib.get("spaceblaster", {}).get("lan", {}).get("players", {}).get("max") == 8,
                  "Bob sees Space Blaster's lan.json settings")
            check(next(g for g in call(47811, "state")["library"] if g["id"] == "spaceblaster")["setup_needed"],
                  "Alice's card asks for the one-time setup")
            check(call(47811, "setup", {"id": "spaceblaster"})["ok"], "setup starts")
            check(wait(lambda: not next(g for g in call(47811, "state")["library"]
                                        if g["id"] == "spaceblaster")["setup_needed"]), "setup finishes and unlocks Play")
            check(lib.get("warcraft2", {}).get("network") == "ipx", "Warcraft II matched the known DB as IPX")
            check(lib.get("warcraft2", {}).get("kind") == "dos", "WAR2.EXE read as a DOS exe")
            check(any(p.startswith("vc2015plus") for p in lib.get("tankduel", {}).get("prereqs", [])),
                  f"Tank Duel needs VC++ 2015+ ({lib.get('tankduel', {}).get('prereqs')})")

            # Party Master: only Alice may start polls, and Bob takes in her poll once he sees she's the master
            r = call(47811, "vote/new", {"title": "Next?"})
            check(r.get("ok") is False and "Party Master" in r.get("error", ""), "no poll before Party Master is on")
            check(call(47811, "settings", {"party_master": True})["ok"] and call(47811, "state")["me"]["party_master"],
                  "Alice turns on Party Master")
            check(wait(lambda: any(p["name"] == "Alice" and p.get("party_master")
                                   for p in call(47812, "state")["peers"])), "Bob sees Alice is the Party Master")
            check(call(47812, "vote/new", {"title": "Bob's"}).get("ok") is False, "Bob (not master) can't start a poll")
            check(call(47811, "state")["scores"] is not None and call(47812, "state")["scores"] is None,
                  "only the Party Master gets the heart scores")
            pid = call(47811, "vote/new", {"title": "Next?"})["id"]
            call(47811, "vote/ballot", {"poll": pid, "ranking": ["tankduel", "spaceblaster"]})
            check(wait(lambda: any(p["id"] == pid for p in call(47812, "state")["polls"])), "poll reached Bob")
            call(47812, "vote/ballot", {"poll": pid, "ranking": ["tankduel"]})
            check(wait(lambda: len(next(p for p in call(47811, "state")["polls"] if p["id"] == pid)["ballots"]) == 2),
                  "Bob's ballot reached Alice")
            win = next(p for p in call(47811, "state")["polls"] if p["id"] == pid)["result"]["winner"]
            check(win == "tankduel", f"both agree winner is tankduel ({win})")

            call(47812, "download", {"id": "spaceblaster"})
            check(wait(lambda: any(d["id"] == "spaceblaster" and d["state"] == "done"
                                   for d in call(47812, "state")["downloads"]), 60), "Space Blaster downloaded to Bob")
            got = os.path.join(gb, "Space Blaster", "data", "big.bin")
            check(os.path.exists(got) and os.path.getsize(got) == 5 * 1024 * 1024 + 123, "downloaded file size matches")
            check(wait(lambda: next(g for g in call(47812, "state")["library"] if g["id"] == "spaceblaster")["have"]),
                  "Bob now owns Space Blaster")

            req = urllib.request.Request("http://127.0.0.1:47811/peer/file/spaceblaster?p=../../dataAlice/settings.json")
            try:
                urllib.request.urlopen(req, timeout=5)
                check(False, "path traversal blocked")
            except urllib.error.HTTPError as e:
                check(e.code == 404, "path traversal blocked")
            pr = call(47812, "prereqs")
            check(len(pr["items"]) > 10, "prereq report lists the catalog")

            # Launcher update: Carol runs an older copy (v1.0.0) in a temp folder and updates from Alice.
            carol = os.path.join(tmp, "carol_install")
            for rel in update.release_files(ROOT):
                dst = os.path.join(carol, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(os.path.join(ROOT, *rel.split("/")), dst)
            init = os.path.join(carol, "lanparty", "__init__.py")
            with open(init, "r", encoding="utf-8") as f:
                src = f.read()
            with open(init, "w", encoding="utf-8", newline="\n") as f:
                f.write(src.replace(f'VERSION = "{VERSION}"', 'VERSION = "1.0.0"'))
            procs.append(subprocess.Popen([sys.executable, "-m", "lanparty", "--port", "47813", "--name", "Carol",
                                           "--data", os.path.join(tmp, "dataCarol"), "--peer", "127.0.0.1:47811",
                                           "--no-browser", "--no-discovery"], cwd=carol))
            new_pid = None
            try:
                check(wait(lambda: call(47813, "state")["update"]["available"].get("name") == "Alice"),
                      "Carol (v1.0.0) is offered Alice's newer launcher")
                av = call(47813, "state")["update"]["available"]
                check(av.get("version") == VERSION, f"offer shows v{VERSION} ({av.get('version')})")
                time.sleep(2)
                check(update.installed_version(carol) == "1.0.0", "nothing is updated without the click")
                old_pid = call(47813, "state")["pid"]
                r = call(47813, "app/update", {"peer_id": av["peer_id"]})
                check(r.get("ok"), f"update starts ({r.get('error')})")

                def restarted():
                    global_state = call(47813, "state")
                    return global_state["pid"] != old_pid and global_state["version"] == VERSION
                check(wait(restarted, 60), "Carol restarted on the new version")
                new_pid = call(47813, "state")["pid"]
                check(update.installed_version(carol) == VERSION, "Carol's files are the new version")
                bks = [d for d in os.listdir(os.path.join(carol, "backup")) if d.endswith("_update")]
                with open(os.path.join(carol, "backup", bks[0], "lanparty", "__init__.py"), encoding="utf-8") as f:
                    check('VERSION = "1.0.0"' in f.read(), "Carol's old files backed up in backup\\<ts>_update")
                check(not call(47813, "state")["update"]["available"], "no update offered once on the same version")
            except Exception as e:  # noqa: BLE001
                check(False, f"launcher update ({e})")
            finally:
                try:
                    new_pid = new_pid or call(47813, "state")["pid"]
                except Exception:  # noqa: BLE001
                    pass
                if new_pid:
                    try:
                        os.kill(new_pid, signal.SIGTERM)
                    except OSError:
                        pass
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                p.wait(10)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL_PASS" if not fails else f"FAILED: {len(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
