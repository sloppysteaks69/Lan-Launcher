"""Starts a game, natively or through DOSBox, as the party host, a joining client, or solo.

A game's lan.json can give exact host/join command lines; {host_ip}, {my_ip}, {player_name}, {game_dir} are
filled in. {my_ip} is this PC's own address: the VPN IP during an internet party, else the default-route LAN IP."""
import os
import re
import shlex
import socket
import subprocess
import threading

from . import forcebind, namewrite

THEN_JOIN_DELAY = 4.0


def _split(args: str) -> list:
    try:
        return shlex.split(args, posix=False)
    except ValueError:
        return args.split()


def clean_name(name: str) -> str:
    """A player name safe to pass as one argument: no quotes, control or shell characters."""
    return re.sub(r'[^A-Za-z0-9 _\-\.\[\]()]', "", name or "").strip()[:32] or "Player"


def resolve_in_game(game: dict, rel: str) -> str:
    """Full path of a file inside the game folder. Raises ValueError for anything outside it or missing."""
    base = os.path.realpath(game["path"])
    if not rel or rel.lower() in ("exe", "{exe}"):
        rel = game.get("exe") or ""
    full = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([base, full]) != base:
        raise ValueError(f"'{rel}' is outside the game folder")
    if not os.path.isfile(full):
        raise ValueError(f"'{rel}' not found in the game folder")
    return full


def _wrap(exe: str, args: list) -> list:
    if exe.lower().endswith((".bat", ".cmd")):
        return ["cmd.exe", "/c", exe] + args
    if exe.lower().endswith(".ps1"):
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe] + args
    return [exe] + args


def default_ip() -> str:
    """This PC's default-route LAN IP (UDP connect trick, nothing is sent), else 127.0.0.1."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def resolve_my_ip(my_ip: str = "") -> str:
    """{my_ip} is never empty: the given (VPN) IP, else the LAN IP, else 127.0.0.1."""
    return (my_ip or "").strip() or default_ip()


def build_argv(game: dict, role: str, host_ip: str = "", player_name: str = "", my_ip: str = ""):
    """(argv, cwd) for role solo|host|join. Raises ValueError when the game can't be started that way."""
    subs = {"{host_ip}": host_ip or "", "{my_ip}": resolve_my_ip(my_ip), "{player_name}": clean_name(player_name),
            "{game_dir}": game["path"]}

    def fill(s: str) -> str:
        for k, v in subs.items():
            s = s.replace(k, v)
        return s

    spec = (game.get("lan") or {}).get(role) if role in ("host", "join") else None
    if spec:
        exe = resolve_in_game(game, spec.get("command", ""))
        args = [fill(a) for a in spec.get("args", [])]
    else:
        exe = resolve_in_game(game, game.get("exe") or "")
        args = game.get("args", "")
        if role == "host" and game.get("host_args"):
            args = (args + " " + game["host_args"]).strip()
        if role == "join" and game.get("join_args"):
            args = (args + " " + game["join_args"]).strip()
        args = [fill(a) for a in _split(args)]
    return _wrap(exe, args), os.path.dirname(exe)


def game_exe_paths(game: dict) -> list:
    """Full paths of the programs this game runs (its exe and any lan.json host/join command), for the firewall."""
    out = []
    for rel in [game.get("exe") or ""] + [((game.get("lan") or {}).get(r) or {}).get("command", "")
                                          for r in ("host", "join")]:
        if not rel or not game.get("path"):
            continue
        try:
            p = resolve_in_game(game, rel)
        except ValueError:
            continue
        if p.lower().endswith(".exe") and p not in out:
            out.append(p)
    return out


# ---- starting games as the normal user ----
# The launcher runs elevated (run_admin.bat; n2n needs it), and a child process inherits that. Dark Reign 2
# crashed creating a LAN game when started that way, and fine when double-clicked. So when elevated, games are
# started with the desktop user's own (medium-integrity) token, taken from the shell (explorer.exe).

def is_elevated() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _popen_unelevated(argv: list, cwd: str) -> int:
    """Start argv in cwd with the shell's token (CreateProcessWithTokenW). Returns the pid; raises OSError."""
    import ctypes
    from ctypes import wintypes
    u32, k32, adv = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    hwnd = u32.GetShellWindow()
    if not hwnd:
        raise OSError("no desktop shell window")
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    hproc = k32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not hproc:
        raise OSError(f"OpenProcess(shell) failed: {ctypes.GetLastError()}")
    htok, hdup = wintypes.HANDLE(), wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(wintypes.HANDLE(hproc), 0x0002, ctypes.byref(htok)):   # TOKEN_DUPLICATE
            raise OSError(f"OpenProcessToken failed: {ctypes.GetLastError()}")
        # TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID
        if not adv.DuplicateTokenEx(htok, 0x0008 | 0x0002 | 0x0001 | 0x0080 | 0x0100, None, 2, 1,
                                    ctypes.byref(hdup)):                       # SecurityImpersonation, TokenPrimary
            raise OSError(f"DuplicateTokenEx failed: {ctypes.GetLastError()}")

        class STARTUPINFO(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        si = STARTUPINFO()
        si.cb = ctypes.sizeof(si)
        pi = PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        if not adv.CreateProcessWithTokenW(hdup, 0, None, cmd, 0x00000400, None, cwd,   # CREATE_UNICODE_ENVIRONMENT
                                           ctypes.byref(si), ctypes.byref(pi)):
            raise OSError(f"CreateProcessWithTokenW failed: {ctypes.GetLastError()}")
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        return int(pi.dwProcessId)
    finally:
        for h in (hdup, htok):
            if h.value:
                k32.CloseHandle(h)
        k32.CloseHandle(wintypes.HANDLE(hproc))


def wants_admin(game: dict, cfg) -> bool:
    """Per-PC opt-out (settings 'run_as_admin': [game ids]). Not read from lan.json: that file can come from someone
    else's PC, and it must not be able to ask for admin rights."""
    try:
        return game.get("id") in ((cfg.get("run_as_admin", []) if cfg is not None else []) or [])
    except Exception:
        return False


def start(argv: list, cwd: str, as_admin: bool = False, info: list = None) -> str:
    """Start a game process. Elevated launcher: as the normal user unless as_admin; falls back to a plain start.
    Returns 'user', 'admin' or 'normal' and logs the way into info."""
    progs = [a for a in argv if a.lower().endswith((".exe", ".bat", ".cmd", ".ps1"))
             and os.path.basename(a).lower() not in ("cmd.exe", "powershell.exe") + forcebind.LOADERS]
    name = os.path.basename(progs[0] if progs else (argv[0] if argv else "?"))
    if is_elevated() and not as_admin:
        try:
            _popen_unelevated(argv, cwd)
            if info is not None:
                info.append(f"Started {name} as your normal user (not administrator).")
            return "user"
        except Exception as e:
            subprocess.Popen(argv, cwd=cwd)
            if info is not None:
                info.append(f"Started {name} as administrator: starting it as your normal user failed ({e}).")
            return "admin"
    subprocess.Popen(argv, cwd=cwd)
    if info is not None and is_elevated():
        info.append(f"Started {name} as administrator (run_as_admin is set for this game).")
    return "admin" if is_elevated() else "normal"


def setup_needed(game: dict, cfg) -> bool:
    s = (game.get("lan") or {}).get("setup")
    return bool(s) and cfg.get("setup_done", {}).get(game["id"]) != s


def run_setup(game: dict, cfg, done_cb=None) -> str:
    """Runs the game's run-once setup script (not elevated) and remembers it on success."""
    script = (game.get("lan") or {}).get("setup")
    if not script:
        return "This game has no setup script."
    try:
        full = resolve_in_game(game, script)
    except ValueError as e:
        return str(e)
    argv = _wrap(full, [])

    def wait() -> None:
        code = subprocess.Popen(argv, cwd=os.path.dirname(full)).wait()
        if code == 0:
            done = dict(cfg.get("setup_done", {}))
            done[game["id"]] = script
            cfg.set("setup_done", done)
        if done_cb:
            done_cb(game, code)
    threading.Thread(target=wait, daemon=True).start()
    return "ok"


def launch(game: dict, net, role: str = "solo", host_ip: str = "", player_name: str = "", cfg=None,
           notes: list = None, info: list = None, my_ip: str = "") -> str:
    """role: solo | host | join. Returns 'ok' or an error message. Non-fatal problems go into notes; how the game
    was started (normal user or administrator) goes into info. my_ip: this PC's VPN IP during an internet party
    (empty: the LAN IP is used for {my_ip})."""
    as_admin = wants_admin(game, cfg)
    given_ip = (my_ip or "").strip()
    my_ip = resolve_my_ip(given_ip)
    if not game.get("exe") and not (game.get("lan") or {}).get(role):
        return "No program found in this game's folder. Add a lan.json with \"exe\"."
    if cfg is not None and setup_needed(game, cfg):
        return "This game needs its one-time setup first (Setup button on its card)."
    # Games that can't take a name (or pick the wrong adapter) get it written into their own settings first.
    problems = namewrite.apply(game, clean_name(player_name), my_ip)
    if notes is not None:
        notes.extend(problems)
    lan = game.get("lan") or {}
    if game.get("kind") == "dos" and not lan.get(role):
        db = net.dosbox_path()
        if not db:
            return ("This is a DOS game, and the bundled DOSBox-X is missing from "
                    "lanparty\\bin\\dosbox-x. Restore it, or set a DOSBox path in Settings.")
        args = game.get("join_args" if role == "join" else "host_args" if role == "host" else "args", "") or ""
        conf = net.dosbox_conf(game, role, host_ip,
                               args.replace("{host_ip}", host_ip or "").replace("{my_ip}", my_ip))
        start(net.dosbox_argv(db, conf), game["path"], as_admin, info)
        return "ok"
    if game.get("kind") == "win16" and not lan.get(role):
        return "This is a 16-bit Windows game; 64-bit Windows can't run it directly (try otvdm or a VM)."
    if game.get("network") == "ipx" and role != "solo" and game.get("kind") == "pe":
        if game.get("arch") == "x64":
            # IPXWrapper is 32-bit only; a 64-bit build (e.g. a source port) plays over TCP/IP, so just start it.
            if info is not None:
                info.append("64-bit game: IPX wrapper skipped (64-bit games use TCP/IP).")
        elif not net.wrapper_installed(game):
            r = net.install_wrapper(game)
            if r != "ok":
                return "IPX wrapper: " + r
    try:
        argv, cwd = build_argv(game, role, host_ip, player_name, my_ip)
    except ValueError as e:
        return str(e)
    # In an internet party (given_ip = the VPN IP), native games go through ForceBindIP so they use the VPN address.
    data_dir = getattr(cfg, "data_dir", "") if cfg is not None else ""
    argv = forcebind.maybe_wrap(game, argv, given_ip, cfg, data_dir, info)
    start(argv, cwd, as_admin, info)
    # A dedicated-server host command can ask the launcher to start a normal client that joins it too.
    # A game bound to {my_ip} (-ip) can't be reached on loopback, so join that address instead.
    host_spec = lan.get("host") or {}
    if role == "host" and host_spec.get("then_join") and lan.get("join"):
        binds = given_ip or any("{my_ip}" in str(x) for x in host_spec.get("args", []))
        self_ip = my_ip if binds else "127.0.0.1"

        def join_self() -> None:
            try:
                a, c = build_argv(game, "join", self_ip, player_name, my_ip)
                start(forcebind.maybe_wrap(game, a, given_ip, cfg, data_dir, None), c, as_admin, info)
            except (ValueError, OSError):
                pass
        threading.Timer(THEN_JOIN_DELAY, join_self).start()
    return "ok"
