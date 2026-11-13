"""Unit tests for writing the player name into game settings. Run: python tests\\test_namewrite.py"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import namewrite  # noqa: E402
from lanparty.launcher import clean_name  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


tmp = tempfile.mkdtemp(prefix="lanparty_name_")
try:
    gdir = os.path.join(tmp, "Game")
    os.makedirs(os.path.join(gdir, "cfg"))
    ini = os.path.join(gdir, "player.ini")
    with open(ini, "w", encoding="latin-1", newline="") as f:
        f.write("[Video]\r\nWidth=640\r\n\r\n[Player]\r\nName=OldName\r\nTeam=1\r\n\r\n[Sound]\r\nVol=5\r\n")
    with open(os.path.join(gdir, "cfg", "config.cfg"), "w", newline="") as f:
        f.write('bind w +forward\nseta name "Someone"\nseta fov 90\n')
    u16 = os.path.join(gdir, "u16.ini")
    with open(u16, "w", encoding="utf-16", newline="") as f:
        f.write("[Net]\r\nPort=7777\r\n")

    entries = namewrite.clean_entries([
        {"ini": "player.ini", "section": "Player", "key": "Name", "value": "{player_name}"},
        {"ini": "player.ini", "section": "Lobby", "key": "Nick", "value": "[LAN] {player_name}"},
        {"text": "cfg\\config.cfg", "pattern": r"^seta name .*$", "line": 'seta name "{player_name}"'},
        {"ini": "u16.ini", "section": "Net", "key": "Name", "value": "{player_name}"},
        {"ini": "..\\..\\evil.ini", "section": "a", "key": "b", "value": "{player_name}"},
        {"ini": "game.exe", "section": "a", "key": "b", "value": "{player_name}"},
        {"registry": "HKLM\\Software\\Evil", "value": "x", "data": "{player_name}"},
        {"ini": "player.ini", "section": "Paths", "key": "Exe", "value": "C:\\evil.exe"},
        {"ini": "{appdata}\\..\\..\\evil.ini", "section": "a", "key": "b", "value": "{player_name}"},
        {"nonsense": True},
    ])
    check(len(entries) == 9, f"bad entry dropped by clean_entries ({len(entries)})")
    game = {"title": "Test", "path": gdir, "lan": {"name": entries}}
    name = clean_name('Player "the" Great')
    problems = namewrite.apply(game, name)

    text = open(ini, encoding="latin-1", newline="").read()
    check("Name=Player the Great\r\nTeam=1" in text, "ini: existing key replaced in its section")
    check("[Lobby]\r\nNick=[LAN] Player the Great" in text, "ini: missing section and key added")
    check(text.startswith("[Video]\r\nWidth=640\r\n\r\n[Player]") and "[Sound]\r\nVol=5" in text, "ini: other lines untouched, CRLF kept")
    check(os.path.exists(ini + ".lanparty-bak"), "ini: original backed up")
    cfg = open(os.path.join(gdir, "cfg", "config.cfg"), newline="").read()
    check(cfg == 'bind w +forward\nseta name "Player the Great"\nseta fov 90\n', "text: matching line replaced, LF kept")
    u = open(u16, encoding="utf-16").read()
    check("Name=Player the Great" in u and open(u16, "rb").read()[:2] == b"\xff\xfe", "UTF-16 ini stays UTF-16")
    check(sum("must stay inside" in p for p in problems) == 2, "..\\ escapes from game folder and AppData refused")
    check(any("not a config file type" in p for p in problems), "non-config file refused")
    check(any("HKCU" in p for p in problems), "HKLM registry write refused")
    check(any("must contain {player_name}" in p for p in problems) and "evil.exe" not in text,
          "value without {player_name} refused")
    check(len(problems) == 5, f"exactly the 5 bad entries reported ({problems})")
    check(not os.path.exists(os.path.join(tmp, "evil.ini")) and not os.path.exists(os.path.join(os.environ.get("TEMP", tmp), "evil.ini")),
          "no stray evil.ini written anywhere")

    before = open(ini, "rb").read()
    namewrite.apply(game, name)
    check(open(ini, "rb").read() == before, "running twice changes nothing")

    # {my_ip}: AoE3 picks the wrong adapter unless Startup\user.cfg says OverrideAddress="<VPN IP>"
    aoe = {"title": "AoE3", "path": gdir, "lan": {"name": namewrite.clean_entries([
        {"text": "Startup\\user.cfg", "pattern": r"^OverrideAddress=.*$", "line": 'OverrideAddress="{my_ip}"'}])}}
    ucfg = os.path.join(gdir, "Startup", "user.cfg")
    p = namewrite.apply(aoe, name, "10.128.0.10")
    check(not p and open(ucfg, newline="").read() == 'OverrideAddress="10.128.0.10"\r\n',
          f"{{my_ip}}: missing file and folder created with the address ({p})")
    with open(ucfg, "a", newline="") as f:
        f.write("+nointromovies\r\n")
    p = namewrite.apply(aoe, name, "10.128.0.42")
    check(not p and open(ucfg, newline="").read() == 'OverrideAddress="10.128.0.42"\r\n+nointromovies\r\n',
          "{my_ip}: a new party's address replaces the old line, other lines kept")
    p = namewrite.apply(aoe, name, "")
    check(p and "address isn't known" in p[0] and "10.128.0.42" in open(ucfg).read(), "{my_ip}: no address, no write")
    p = namewrite.apply(aoe, name, '1.2.3.4"\r\n+evil')
    check(p and "+evil" not in open(ucfg).read(), "{my_ip}: only a plain IPv4 is written")
    both = {"title": "B", "path": gdir, "lan": {"name": namewrite.clean_entries([
        {"ini": "net.ini", "section": "Net", "key": "Who", "value": "{player_name}@{my_ip}"}])}}
    namewrite.apply(both, name, "10.0.0.5")
    check("Who=Player the Great@10.0.0.5" in open(os.path.join(gdir, "net.ini")).read(), "{player_name} and {my_ip} together")
    exe_e = {"title": "X", "path": gdir, "lan": {"name": namewrite.clean_entries([
        {"text": "Startup\\evil.exe", "pattern": "^x$", "line": "{my_ip}"}])}}
    check(any("not a config file type" in x for x in namewrite.apply(exe_e, name, "10.0.0.5")),
          "{my_ip}: still only config files")

    if sys.platform == "win32":
        import winreg
        key = r"Software\LanPartyLauncherTest"
        reg_game = {"title": "Reg", "path": gdir, "lan": {"name": namewrite.clean_entries(
            [{"registry": "HKCU\\" + key, "value": "PlayerName", "data": "{player_name}"}])}}
        p = namewrite.apply(reg_game, name)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as h:
            val = winreg.QueryValueEx(h, "PlayerName")[0]
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        check(not p and val == "Player the Great", "HKCU registry value written")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL_PASS" if not fails else f"FAILED: {len(fails)}")
sys.exit(1 if fails else 0)
