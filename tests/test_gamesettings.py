"""Tests for the per-game settings (the gear): exe choice, extra arguments, sharing through lan.json, per-PC switches.
Run: python tests\\test_gamesettings.py"""
import glob
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import launcher  # noqa: E402
from lanparty.config import Config  # noqa: E402
from lanparty.discovery import Discovery  # noqa: E402
from lanparty.library import Library  # noqa: E402
from lanparty.netwrap import NetWrap  # noqa: E402
from lanparty.peers import PeerSync  # noqa: E402
from lanparty.server import App, write_lan_json  # noqa: E402
from lanparty.transfer import Downloads  # noqa: E402
from lanparty.vote import Votes  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


def settle(app):
    for _ in range(100):
        if not app.library.scanning:
            break
        time.sleep(0.05)
    app.library.rescan()


tmp = tempfile.mkdtemp(prefix="lp_gear_")
try:
    games = os.path.join(tmp, "Games")
    gdir = os.path.join(games, "Universe-at-War-Earth")
    os.makedirs(os.path.join(gdir, "Tools"))
    for rel, size in (("UAWEA.exe", 9000), ("LaunchUAW.exe", 1500), ("Tools\\Setup.exe", 10), ("readme.txt", 5)):
        with open(os.path.join(gdir, rel), "wb") as f:
            f.write(b"\0" * size)
    cfg = Config(os.path.join(tmp, "data"))
    cfg.set("folders", [games])
    lib = Library(cfg)
    disc = Discovery(cfg, 47801)
    votes = Votes(cfg)
    sync = PeerSync(cfg, disc, lib, votes)
    app = App(cfg, lib, disc, sync, votes, Downloads(cfg, disc, lib, sync), NetWrap(cfg))
    lib.rescan()
    g = lib.get("universeatwarearth")
    rels = sorted(e["rel"] for e in g["exes"])
    check(rels == ["LaunchUAW.exe", "Tools\\Setup.exe", "UAWEA.exe"], f"every exe in the folder listed, real case ({rels})")
    check(g["exe"] == g["auto_exe"] == "LaunchUAW.exe" and g["exe_source"] == "automatic",
          "automatic pick follows known_games.json's exe hint (LaunchUAW.exe, confirmed in testing)")
    hints = {e["rel"]: e["hint"] for e in g["exes"]}
    check(hints["LaunchUAW.exe"] == "launcher" and hints["Tools\\Setup.exe"] == "setup / uninstaller", "hints")
    check("exes" not in lib.public_list()[0], "the exe list isn't sent to peers")

    # ---- this PC: choose the launcher, add arguments ----
    r = app.save_game_settings({"id": g["id"], "exe": "uawea.exe", "args": "-windowed\n -nomovies",
                                "run_as_admin": True, "force_bind": False})
    settle(app)
    g = lib.get("universeatwarearth")
    check(r["ok"] and g["exe"] == "UAWEA.exe" and g["exe_source"] == "this PC", "exe chosen for this PC")
    check(g["args"] == "-windowed -nomovies" and g["args_source"] == "this PC", "arguments, one line")
    check(cfg.get("run_as_admin") == [g["id"]] and cfg.get("no_force_bind") == [g["id"]], "admin / ForceBindIP switches")
    argv, cwd = launcher.build_argv(g, "solo")
    check(argv[0].endswith("UAWEA.exe") and argv[1:] == ["-windowed", "-nomovies"], f"Launch uses them ({argv})")
    check(not os.path.exists(os.path.join(gdir, "lan.json")), "not shared: no lan.json written")

    # ---- refused ----
    check(not app.save_game_settings({"id": g["id"], "exe": "..\\..\\evil.exe"})["ok"], "exe outside the folder refused")
    check(not app.save_game_settings({"id": g["id"], "exe": "C:\\Windows\\notepad.exe"})["ok"], "absolute path refused")
    check(not app.save_game_settings({"id": "nope", "exe": ""})["ok"], "unknown game refused")
    cfg.set("game_overrides", {g["id"]: {"exe": "..\\evil.exe"}})     # a hand-edited bad override is ignored
    settle(app)
    check(lib.get(g["id"])["exe"] == "LaunchUAW.exe", "bad override in settings.json ignored")

    # ---- share through lan.json ----
    with open(os.path.join(gdir, "lan.json"), "w", encoding="utf-8") as f:
        json.dump({"game": "Universe at War", "players": {"min": 2, "max": 8}}, f)
    r = app.save_game_settings({"id": g["id"], "exe": "LaunchUAW.exe", "args": "-x", "share": True})
    settle(app)
    with open(os.path.join(gdir, "lan.json"), encoding="utf-8") as f:
        lj = json.load(f)
    g = lib.get(g["id"])
    check(r["ok"] and lj == {"game": "Universe at War", "players": {"min": 2, "max": 8}, "exe": "LaunchUAW.exe", "args": "-x"},
          f"shared: exe/args in lan.json, other keys kept ({lj})")
    check(glob.glob(os.path.join(gdir, "lan.json.bak-*")), "old lan.json backed up")
    check(g["exe"] == "LaunchUAW.exe" and g["exe_source"] == "lan.json" and g["args_source"] == "lan.json",
          "this PC now follows the shared lan.json")
    check(g["id"] not in (cfg.get("game_overrides") or {}), "per-PC override dropped when shared")
    r = app.save_game_settings({"id": g["id"], "exe": "", "args": "", "share": True})
    with open(os.path.join(gdir, "lan.json"), encoding="utf-8") as f:
        lj = json.load(f)
    check("exe" not in lj and "args" not in lj and lj["players"]["max"] == 8, "Default + empty removes the keys")
    with open(os.path.join(gdir, "lan.json"), "w") as f:
        f.write("{broken")
    r = write_lan_json(gdir, "UaW", "UAWEA.exe", "")
    check(r != "ok" and open(os.path.join(gdir, "lan.json")).read() == "{broken", "invalid lan.json left alone")
    os.remove(os.path.join(gdir, "lan.json"))
    check(write_lan_json(gdir, "UaW", "UAWEA.exe", "") == "ok" and
          json.load(open(os.path.join(gdir, "lan.json"))) == {"game": "UaW", "exe": "UAWEA.exe"}, "new lan.json created")

    # ---- per-PC Default goes back to automatic / lan.json ----
    os.remove(os.path.join(gdir, "lan.json"))
    app.save_game_settings({"id": g["id"], "exe": "", "args": "", "run_as_admin": False, "force_bind": True})
    settle(app)
    g = lib.get(g["id"])
    check(g["exe"] == "LaunchUAW.exe" and g["exe_source"] == "automatic" and not cfg.get("run_as_admin")
          and not cfg.get("no_force_bind"), "Default resets this PC's choices")
    check("exes" in app.state()["library"][0] and "run_as_admin" in app.state()["library"][0], "state() gives the dialog its data")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
