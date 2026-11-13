"""During an internet party, starts native Windows games through ForceBindIP, so all their network traffic uses
this PC's VPN address. Many old games pick the wrong network adapter by themselves (usually Wi-Fi): they show the
wrong IP, and joins fail. ForceBindIP fixes that for any game without a per-game setting.

ForceBindIP (r1ch.net, by Richard Stanway; freeware, closed source, code-signed) starts the game suspended and
injects BindIP.dll, which makes bind/connect/sendto use the given address. It isn't bundled (no redistribution
terms): the launcher fetches the pinned 1.32 zip from r1ch.net on first use and checks every file's SHA-256.
ForceBindIP.exe + BindIP.dll for 32-bit games, ForceBindIP64.exe + BindIP64.dll for 64-bit ones. It needs the
Visual C++ 2015-2022 runtime (x86 and/or x64).

Compatibility first: on by default for every native game in a party; off per game with
lan.json "force_bind": false, or per PC with settings "no_force_bind": [game ids] (the toggle on the game card).
Not used for DOS/DOSBox games, .bat/.ps1 commands, IPX games (IPXWrapper picks its own adapters), or outside a
party. If it can't be fetched or a hash doesn't match, the game starts normally and the notes say so."""
import hashlib
import io
import os
import urllib.request
import zipfile

from . import pe

VERSION = "1.32"
URL = "https://r1ch.net/assets/forcebindip/ForceBindIP-1.32.zip"
ZIP_SHA256 = "9E52A88262093D3A50C19A5B46ECC22D9F4A95BCC7C2E968FADD5C5942024B1A"
FILES = {"ForceBindIP.exe": "920678EE507AC8AEE6F26A9137F208C48E3AF9EB5AEDC22AAA9B8E92D77B935E",
         "BindIP.dll": "51921AA88F6B872D35B6E495AC1898CD731296A2745755300E240C8CF678DAE4",
         "ForceBindIP64.exe": "7DECCB2683741ECDA2246F268553D2F4C9427006746EB5B232F743D8ADA063E9",
         "BindIP64.dll": "10E347EEBE0CC17897D5A72F7A061D23A81DFE3EA2205F534DC2FF15B43A3438"}
MAX_ZIP = 1 << 20
LOADERS = ("forcebindip.exe", "forcebindip64.exe")


def tools_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "tools", "forcebindip")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def ready(data_dir: str) -> bool:
    d = tools_dir(data_dir)
    for name, want in FILES.items():
        try:
            with open(os.path.join(d, name), "rb") as fh:
                if _sha(fh.read()) != want:
                    return False
        except OSError:
            return False
    return True


def ensure(data_dir: str, fetch=None) -> str:
    """'ok' once the four pinned files are in tools\\forcebindip, else why not. fetch(url) -> bytes is for tests."""
    if ready(data_dir):
        return "ok"
    try:
        if fetch:
            data = fetch(URL)
        else:
            with urllib.request.urlopen(URL, timeout=20) as r:
                data = r.read(MAX_ZIP + 1)
    except Exception as e:  # noqa: BLE001 - any network failure: start the game normally
        return f"couldn't download ForceBindIP from r1ch.net ({e})"
    if len(data) > MAX_ZIP or _sha(data) != ZIP_SHA256:
        return "the ForceBindIP download doesn't match the pinned version 1.32 (SHA-256), not used"
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        members = {os.path.basename(i.filename): i for i in z.infolist()}
        blobs = {name: z.read(members[name]) for name in FILES}
    except (zipfile.BadZipFile, KeyError) as e:
        return f"the ForceBindIP zip is incomplete ({e})"
    if any(_sha(blobs[n]) != h for n, h in FILES.items()):
        return "a ForceBindIP file doesn't match its pinned SHA-256, not used"
    d = tools_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    for name, blob in blobs.items():          # only the four known names: nothing else from the zip is written
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(blob)
    return "ok" if ready(data_dir) else "ForceBindIP files couldn't be saved"


def vc_runtime_ok(arch: str) -> bool:
    """ForceBindIP is built with VS2015: vcruntime140.dll of the game's bitness must be installed."""
    if os.name != "nt":
        return False
    win = os.environ.get("WINDIR", r"C:\Windows")
    if arch == "x64":
        sysdir = os.path.join(win, "Sysnative") if os.path.isdir(os.path.join(win, "Sysnative")) else \
            os.path.join(win, "System32")
    else:
        sysdir = os.path.join(win, "SysWOW64") if os.path.isdir(os.path.join(win, "SysWOW64")) else \
            os.path.join(win, "System32")
    return os.path.isfile(os.path.join(sysdir, "vcruntime140.dll"))


def exe_arch(path: str) -> str:
    try:
        return pe.read_exe(path).get("arch") or ""
    except Exception:  # noqa: BLE001
        return ""


def enabled_for(game: dict, cfg) -> bool:
    """On unless lan.json says "force_bind": false or this PC listed the game in no_force_bind."""
    if (game.get("lan") or {}).get("force_bind") is False:
        return False
    try:
        off = (cfg.get("no_force_bind", []) if cfg is not None else []) or []
    except Exception:  # noqa: BLE001
        off = []
    return game.get("id") not in off


def eligible(game: dict, argv: list) -> str:
    """'' if this start can be force-bound, else the reason it isn't."""
    if not argv or not str(argv[0]).lower().endswith(".exe"):
        return "not a Windows program (script or DOSBox)"
    if os.path.basename(str(argv[0])).lower() in ("cmd.exe", "powershell.exe") + LOADERS:
        return "started through a script"
    if game.get("kind") == "dos":
        return "DOS game"
    if game.get("network") == "ipx":
        return "IPX game (IPXWrapper picks the adapters)"
    return ""


def wrap(argv: list, ip: str, data_dir: str, arch: str, delayed: bool = False) -> list:
    """ForceBindIP[64].exe [-i] <ip> <game.exe> <args...>. -i waits for the game's window before injecting."""
    loader = "ForceBindIP64.exe" if arch == "x64" else "ForceBindIP.exe"
    return [os.path.join(tools_dir(data_dir), loader)] + (["-i"] if delayed else []) + [ip] + list(argv)


def maybe_wrap(game: dict, argv: list, ip: str, cfg, data_dir: str, info: list = None, fetch=None) -> list:
    """argv, through ForceBindIP when it applies (party IP given, enabled, eligible, files and runtime there).
    Every decision that matters to the player goes into info."""
    if not ip or not data_dir:
        return argv
    if not enabled_for(game, cfg):
        if info is not None:
            info.append(f"ForceBindIP is off for {game.get('title', 'this game')}.")
        return argv
    why = eligible(game, argv)
    if why:
        return argv
    arch = exe_arch(argv[0]) or game.get("arch") or "x86"
    if not vc_runtime_ok(arch):
        if info is not None:
            info.append(f"ForceBindIP not used: the Visual C++ 2015-2022 runtime ({arch}) isn't installed. "
                        "Install it (Prereqs tab) if this game uses the wrong network address.")
        return argv
    r = ensure(data_dir, fetch)
    if r != "ok":
        if info is not None:
            info.append(f"ForceBindIP not used: {r}. Started normally.")
        return argv
    delayed = (game.get("lan") or {}).get("force_bind") == "delayed"
    if info is not None:
        info.append(f"{game.get('title', 'Game')} force-bound to {ip} (ForceBindIP {arch}{', delayed' if delayed else ''}). "
                    "If it misbehaves, turn ForceBindIP off on its card.")
    return wrap(argv, ip, data_dir, arch, delayed)
