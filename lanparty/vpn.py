"""n2n VPN integration: download, TAP driver check, supernode/edge management, party code encode/decode.

n2n creates a virtual ethernet switch — every player on the same virtual LAN with broadcast support, encrypted,
with NAT traversal through the supernode. The host runs supernode + edge; everyone else runs edge.

Party code = base64url of: 4-byte IP + 2-byte port + 32-byte secret (38 bytes → 52-char base64, shown as
groups of 4 with dashes for readability)."""
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

# Bundled build: lucktu/n2n6 v2.3 (r287), windows x64 static, in lanparty\bin\. ntop publishes no Windows builds.
# n2n6 flags differ from ntop 3.x: -A4 = ChaCha20 (-A5 is Speck), key max 32 ASCII, supernode uses -l <port>.
# Never pass -b (bypass): it installs the WinDivert kernel driver. Always pass -l, or edge falls back to a public
# supernode (n2n6.ouno.eu.org).
N2N_VERSION = "n2n6 v2.3 r287"
N2N_SHA256 = {"edge.exe": "300B350B1D32BEA86F40F0EEC981584E3895477BAA1B3FCBE28F0A223D3AD399",
              "supernode.exe": "E058B5FEDE26452987E45B11849EEE90DBCE3B51F427FB6D03FE9F1B439A76C9"}
N2N_CIPHER = "-A4"   # ChaCha20 in n2n6

SUPERNODE_PORT = 7654
VPN_SUBNET = "10.128.0"
VPN_MASK = "255.255.255.0"
COMMUNITY_PREFIX = "lp_"
JOIN_IP_TRIES = 5
# TAP MTU: the encrypted n2n packet must fit the internet path, or it fragments and mobile networks drop it.
# Joiners measure the path to the host (vpn.path_mtu) and subtract N2N_OVERHEAD. The overhead is estimated from
# header sizes (outer IP 20 + UDP 8 + n2n header ~46 + ChaCha20 IV 16 + inner Ethernet 14 = ~104) plus a margin;
# measuring it with pings is wrong because n2n6 compresses ping's filler data, and game files don't compress.
N2N_OVERHEAD = 110
MTU_HOST = 1500 - N2N_OVERHEAD   # 1390: the host, and any joiner on a full 1500-byte path (same switch, home lines)
MTU_SAFE = 1280                  # when the host doesn't answer pings
JOIN_REGISTER_WAIT = 15.0   # seconds for the host's supernode to answer a joiner

TAP_ADAPTER_NAME = "LanPartyVPN"
TAP_HWID = "tap0901"            # OpenVPN TAP-Windows6
TAP_INSTALL_URL = "https://build.openvpn.net/downloads/releases/tap-windows-9.24.7-I601-Win10.exe"


_BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")


def _tools_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "n2n")
    os.makedirs(d, exist_ok=True)
    return d


def _edge_path(data_dir: str) -> str:
    bundled = os.path.join(_BUNDLED_DIR, "edge.exe")
    if os.path.isfile(bundled):
        return bundled
    return os.path.join(_tools_dir(data_dir), "edge.exe")


def _supernode_path(data_dir: str) -> str:
    bundled = os.path.join(_BUNDLED_DIR, "supernode.exe")
    if os.path.isfile(bundled):
        return bundled
    return os.path.join(_tools_dir(data_dir), "supernode.exe")


# ---- party code ----

MAX_CODE_LAN_IPS = 2   # host LAN addresses a party code carries (4 bytes, ~7 code characters each)


def encode_party_code(host_ip: str, port: int, secret: bytes, lan_ips=()) -> str:
    """Encode host IP + port + secret (+ the host's LAN IPv4s, for joiners in the same room) into a typeable code.
    Without LAN IPs the code is the 1.1.0 format (38 bytes); each LAN IP adds 4 bytes at the end."""
    parts = host_ip.split(".")
    raw = struct.pack("!BBBBH", *(int(p) for p in parts), port) + secret
    raw += b"".join(socket.inet_aton(ip) for ip in list(lan_ips)[:MAX_CODE_LAN_IPS])
    b = base64.b32encode(raw).decode("ascii").rstrip("=")
    # group into chunks of 4 with dashes
    return "-".join(b[i:i + 4] for i in range(0, len(b), 4))


def decode_party_code_full(code: str) -> tuple:
    """Decode a party code → (host_ip, port, secret, lan_ips). Old (1.1.0) codes have no LAN IPs.
    Raises ValueError on bad input."""
    clean = code.upper().replace("-", "").replace(" ", "")
    # re-pad base32
    pad = (8 - len(clean) % 8) % 8
    raw = base64.b32decode(clean + "=" * pad)
    extra = len(raw) - 38
    if extra < 0 or extra % 4 or extra // 4 > MAX_CODE_LAN_IPS:
        raise ValueError(f"bad party code length ({len(raw)} bytes)")
    a, b, c, d, port = struct.unpack("!BBBBH", raw[:6])
    lan = [socket.inet_ntoa(raw[38 + 4 * i:42 + 4 * i]) for i in range(extra // 4)]
    return f"{a}.{b}.{c}.{d}", port, raw[6:38], lan


def decode_party_code(code: str) -> tuple:
    """Decode a party code → (host_ip, port, secret). Raises ValueError on bad input."""
    return decode_party_code_full(code)[:3]


def _usable_lan_ip(ip: str) -> bool:
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback and not a.is_link_local and not ip.startswith(VPN_SUBNET + ".")


def host_lan_ips(external_ip: str = "") -> list:
    """The host's LAN address for players in the same room: the IPv4 its default route leaves from, if private.
    Only that one: VirtualBox/Hyper-V/WSL adapters (192.168.56.1, 172.17.x) aren't reachable from other PCs."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))      # no packet is sent; it only picks the route
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        return []
    return [ip] if _usable_lan_ip(ip) and ip != external_ip else []


def _same_24(a: str, b: str) -> bool:
    return a.split(".")[:3] == b.split(".")[:3]


def probe_lan_host(ip: str, party_id: str, timeout: float = 1.5) -> bool:
    """True if the launcher at ip answers /peer/hello as the host of this party (VPN .1, same party id)."""
    from . import HTTP_PORT
    try:
        with urllib.request.urlopen(f"http://{ip}:{HTTP_PORT}/peer/hello", timeout=timeout) as r:
            msg = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(msg, dict) and msg.get("app") == "lanparty" and msg.get("party") == party_id
            and f"{VPN_SUBNET}.1" in (msg.get("ips") or []))


def supernode_order(public_ip: str, lan_ips: list, my_ips: list, is_host) -> list:
    """Supernode addresses for a joiner's edge, best first (edge moves to the next when one stops answering).
    A joiner in the host's room can't always reach the host's public IP: many routers don't loop traffic from
    inside back to their own public address. So a host LAN IP on one of our subnets that answers as the party's
    host goes first; one on our subnet that doesn't answer (another network that reuses 192.168.1.x) goes last."""
    same = [ip for ip in lan_ips if _usable_lan_ip(ip) and any(_same_24(ip, m) for m in my_ips)]
    confirmed = [ip for ip in same if is_host(ip)]
    order = confirmed + [public_ip] + [ip for ip in same if ip not in confirmed]
    return list(dict.fromkeys(order))[:2]    # n2n6 keeps a short supernode list; two is all we need


# ---- external IP detection ----

def detect_external_ip() -> str:
    """Try to detect the host's external IPv4 address."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                socket.inet_aton(ip)   # validate
                return ip
        except Exception:
            continue
    return ""


# ---- TAP driver ----

def tap_installed() -> bool:
    """Check if the OpenVPN TAP-Windows6 driver is installed."""
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E972-E325-11CE-BFC1-08002BE10318}",
                            "/s", "/f", TAP_HWID], capture_output=True, text=True, timeout=10)
        return TAP_HWID in r.stdout
    except Exception:
        return False


def _sys_exe(name: str) -> str:
    """Path to a 64-bit System32 tool. 32-bit Python on 64-bit Windows sees System32 redirected to SysWOW64,
    which has no pnputil.exe and only 32-bit PowerShell; Sysnative reaches the real System32."""
    win = os.environ.get("WINDIR", r"C:\Windows")
    native = os.path.join(win, "Sysnative", name)
    return native if os.path.exists(native) else os.path.join(win, "System32", name)


def _program_files() -> str:
    """The 64-bit Program Files, also from 32-bit Python (where ProgramFiles is the (x86) folder)."""
    return os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"


def _ps(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run([_sys_exe(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-Command", cmd],
                          capture_output=True, text=True,
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _tap_adapters() -> list:
    """Every TAP-Windows adapter, including disabled/hidden ones: [{Name, Status, PnPDeviceID}]."""
    if sys.platform != "win32":
        return []
    try:
        r = _ps(f"Get-NetAdapter -IncludeHidden | Where-Object ComponentID -eq '{TAP_HWID}' | "
                "Select-Object Name,Status,PnPDeviceID | ConvertTo-Json -Compress")
        out = r.stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


_tap_state_cache = {"at": 0.0, "state": ""}


def tap_adapter_state(cache_s: float = 10.0) -> str:
    """Our own adapter (LanPartyVPN): 'ok', 'disabled' or 'missing'. Cached: the UI polls status often."""
    if time.time() - _tap_state_cache["at"] < cache_s and _tap_state_cache["state"]:
        return _tap_state_cache["state"]
    mine = [a for a in _tap_adapters() if a.get("Name") == TAP_ADAPTER_NAME]
    if not mine:
        st = "missing"
    elif str(mine[0].get("Status")) in ("Disabled", "Not Present"):
        st = "disabled"
    else:
        st = "ok"
    _tap_state_cache.update(at=time.time(), state=st)
    return st


def ensure_tap_adapter(data_dir: str, log: list) -> str:
    """Make sure our own enabled TAP adapter exists, named LanPartyVPN, so edge never borrows (or trips over) another
    VPN's adapter. Installs the driver, creates the adapter with TAP-Windows' tapinstall.exe, re-enables it if
    disabled. Needs admin. Returns 'ok' or an error string."""
    r = install_tap_driver(data_dir, log)
    if r != "ok":
        return r
    _tap_state_cache["at"] = 0.0
    ads = _tap_adapters()
    mine = [a for a in ads if a.get("Name") == TAP_ADAPTER_NAME]
    if not mine:
        tap_dir = os.path.join(_program_files(), "TAP-Windows")
        tool = os.path.join(tap_dir, "bin", "tapinstall.exe")
        inf = os.path.join(tap_dir, "driver", "OemVista.inf")
        if not (os.path.isfile(tool) and os.path.isfile(inf)):
            return f"TAP-Windows tools not found in {tap_dir}. Reinstall the TAP driver."
        before = {a.get("Name") for a in ads}
        log.append(f"Creating TAP adapter {TAP_ADAPTER_NAME}...")
        try:
            p = subprocess.run([tool, "install", inf, TAP_HWID], capture_output=True, text=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            return f"Creating the TAP adapter failed: {e}"
        new = []
        for _ in range(10):           # the adapter can take a moment to appear
            new = [a for a in _tap_adapters() if a.get("Name") not in before]
            if new:
                break
            time.sleep(1)
        if not new:
            return f"Creating the TAP adapter failed: {(p.stdout + p.stderr).strip()[-200:]}"
        _ps(f"Rename-NetAdapter -Name '{new[0]['Name']}' -NewName '{TAP_ADAPTER_NAME}'")
        mine = [a for a in _tap_adapters() if a.get("Name") == TAP_ADAPTER_NAME]
        if not mine:
            return f"Created a TAP adapter but could not rename it to {TAP_ADAPTER_NAME}."
    a = mine[0]
    if str(a.get("Status")) in ("Disabled", "Not Present"):
        log.append(f"Enabling TAP adapter {TAP_ADAPTER_NAME}...")
        try:
            subprocess.run([_sys_exe("pnputil.exe"), "/enable-device", str(a.get("PnPDeviceID"))], capture_output=True,
                           text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            return f"Enabling the TAP adapter failed: {e}"
        time.sleep(2)
    _tap_state_cache["at"] = 0.0
    if tap_adapter_state() != "ok":
        return f"TAP adapter {TAP_ADAPTER_NAME} is still disabled. Enable it in Control Panel > Network Connections."
    log.append(f"TAP adapter {TAP_ADAPTER_NAME} ready.")
    return "ok"


def _ping_df(host: str, payload: int) -> bool:
    """One ping with Don't Fragment set. 'TTL=' only appears in a real echo reply (not in 'needs to be fragmented')."""
    try:
        r = subprocess.run([_sys_exe("PING.EXE"), "-n", "1", "-f", "-l", str(payload), "-w", "1500", host],
                           capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "TTL=" in r.stdout.upper()
    except Exception:
        return False


def path_mtu(host: str) -> int:
    """Largest IP packet that crosses the internet path to host unfragmented (binary search with DF pings), or 0 if
    the host doesn't answer pings at all."""
    if sys.platform != "win32" or not host:
        return 0
    if _ping_df(host, 1472):
        return 1500
    lo, hi = 1172, 1472          # payload sizes; +28 for the IP and ICMP headers
    if not _ping_df(host, lo):
        return 0
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _ping_df(host, mid):
            lo = mid
        else:
            hi = mid
    return lo + 28


def vpn_mtu_for(path: int) -> int:
    """TAP MTU that keeps an encrypted n2n packet inside a path of this size."""
    if not path:
        return MTU_SAFE
    return max(MTU_SAFE, min(MTU_HOST, path - N2N_OVERHEAD))


def _q(s: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def _set_tap_mtu(log: list, mtu: int, alias: str = TAP_ADAPTER_NAME) -> None:
    """At MTU 1500, every full-size frame plus n2n's header and encryption is bigger than the internet path, so it
    gets fragmented, and mobile networks drop fragments: the first phone test ran at 0.5 Mbps on a 28 Mbps line."""
    try:
        r = _ps(f"(Get-NetIPInterface -InterfaceAlias {_q(alias)} -AddressFamily IPv4).NlMtu")
        if r.stdout.strip() == str(mtu):
            return
        subprocess.run([_sys_exe("netsh.exe"), "interface", "ipv4", "set", "subinterface", alias,
                        f"mtu={mtu}", "store=persistent"], capture_output=True, text=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.append(f"{alias} MTU set to {mtu}.")
    except Exception as e:
        log.append(f"Could not set the {alias} MTU: {e}")


def tap_adapter_exists() -> bool:
    """Check if our named TAP adapter exists."""
    try:
        r = subprocess.run(["netsh", "interface", "show", "interface"], capture_output=True, text=True, timeout=10)
        return TAP_ADAPTER_NAME in r.stdout
    except Exception:
        return False


def install_tap_driver(data_dir: str, log: list) -> str:
    """Download and install the TAP driver. Returns 'ok' or an error string. Needs admin."""
    if tap_installed():
        log.append("TAP driver already installed.")
        return "ok"
    installer = os.path.join(_tools_dir(data_dir), "tap-windows.exe")
    if not os.path.exists(installer):
        log.append("Downloading TAP driver installer...")
        try:
            with urllib.request.urlopen(TAP_INSTALL_URL, timeout=60) as r:
                with open(installer, "wb") as f:
                    f.write(r.read())
        except Exception as e:
            return f"TAP download failed: {e}"
    log.append("Installing TAP driver (admin prompt)...")
    try:
        r = subprocess.run([installer, "/S"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return f"TAP installer exited with code {r.returncode}"
    except Exception as e:
        return f"TAP install failed: {e}"
    log.append("TAP driver installed.")
    return "ok"


# ---- n2n download ----

def n2n_bundled() -> bool:
    return (os.path.isfile(os.path.join(_BUNDLED_DIR, "edge.exe")) and
            os.path.isfile(os.path.join(_BUNDLED_DIR, "supernode.exe")))


def n2n_ready(data_dir: str) -> bool:
    return n2n_bundled()


def party_id_for(secret: bytes) -> str:
    """Host and joiners derive the same id (and community) from the shared secret."""
    return hashlib.sha256(b"lanparty-id|" + secret).hexdigest()[:8]


def n2n_key_for(secret: bytes) -> str:
    """n2n6 takes at most 32 ASCII chars: 24 bytes of a keyed hash as base64url = 32 chars (192 bits)."""
    k = hashlib.sha256(b"lanparty-n2n-key|" + secret).digest()[:24]
    return base64.urlsafe_b64encode(k).decode("ascii")


ADMIN_NEEDED = ("Internet party needs administrator rights (n2n's edge.exe requires them). Close the launcher and "
                "start it with D:\\LanPartyLauncher\\run_admin.bat.")


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_job = None   # Windows job object: n2n processes in it die when the launcher exits (handle closes with the process)


def _kill_on_exit(proc: subprocess.Popen) -> None:
    """Tie an n2n process to the launcher's lifetime. Closing the launcher's console used to leave edge and supernode
    running, holding the LanPartyVPN adapter, so the next edge borrowed another VPN's adapter."""
    global _job
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        if _job is None:
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            job = k32.CreateJobObjectW(None, None)
            if not job or not _set_job_flags(job, 0x2000):   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                return
            _job = job
        k32.AssignProcessToJobObject(wintypes.HANDLE(_job), wintypes.HANDLE(int(proc._handle)))
    except Exception:
        pass


def _set_job_flags(job, flags: int) -> bool:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = EXTENDED()
    info.Basic.LimitFlags = flags
    return bool(ctypes.windll.kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info),
                                                               ctypes.sizeof(info)))


def release_n2n_for_handoff() -> bool:
    """Update restart: let edge/supernode outlive this launcher (the next one adopts them). True if they will."""
    if sys.platform != "win32" or _job is None:
        return sys.platform == "win32"     # no job: nothing would kill them
    try:
        return _set_job_flags(_job, 0)
    except Exception:
        return False


def kill_stale_n2n(log: list = None) -> int:
    """Stop edge/supernode processes running from our own bin folder (left by a launcher that was closed or crashed).
    Only our bundled exes; another app's n2n is left alone."""
    if sys.platform != "win32":
        return 0
    pattern = os.path.join(_BUNDLED_DIR, "*").replace("'", "''")
    try:
        r = _ps("$n = 0; Get-CimInstance Win32_Process -Filter \"Name='edge.exe' OR Name='supernode.exe'\" | "
                f"Where-Object {{ $_.ExecutablePath -like '{pattern}' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; $n")
        n = int((r.stdout.strip().splitlines() or ["0"])[-1])
    except Exception:
        n = 0
    if n and log is not None:
        log.append(f"Stopped {n} leftover n2n process(es) from an earlier session.")
        time.sleep(1)               # let the TAP adapter be released
    return n


def _spawn(cmd: list, log_path: str, env: dict = None) -> subprocess.Popen:
    """Start an n2n process with its output going to a log file. (Pipes nobody reads fill up and stall n2n.)"""
    with open(log_path, "wb") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _kill_on_exit(proc)
    return proc


def _log_tail(path: str, n: int = 400) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - n))
            return fh.read().decode(errors="replace").strip()
    except OSError:
        return ""


def _set_vpn_private(log: list, alias: str = TAP_ADAPTER_NAME) -> None:
    """Windows files the VPN adapter as a Public network, where most game and launcher firewall rules don't apply.
    Everyone on it holds the party key, so it is a Private network (like a home LAN). The profile only exists once
    the adapter is up, so retry for a few seconds."""
    for _ in range(10):
        r = _ps(f"$p = Get-NetConnectionProfile -InterfaceAlias {_q(alias)} -ErrorAction SilentlyContinue; "
                "if ($p) { if ($p.NetworkCategory -ne 'Private') { Set-NetConnectionProfile -InterfaceAlias "
                f"{_q(alias)} -NetworkCategory Private }}; 'ok' }}")
        if "ok" in r.stdout:
            log.append(f"{alias} set to a Private network (firewall).")
            return
        time.sleep(1)
    log.append(f"Could not set {alias} to Private; games may be blocked by the firewall.")


def vpn_adapter_for(ip: str, tries: int = 10) -> dict:
    """The adapter that really holds our VPN address: {'alias', 'index'}. n2n6 can open another TAP adapter than
    the one passed with -d (seen on a test PC: 10.128.0.1 on 'Local Area Connection'), and then every per-adapter
    setting must go to that one. Falls back to LanPartyVPN if the address can't be found."""
    for _ in range(max(1, tries)):
        try:
            r = _ps(f"Get-NetIPAddress -IPAddress {_q(ip)} -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                    "Select-Object -First 1 InterfaceIndex,InterfaceAlias | ConvertTo-Json -Compress", timeout=15)
            out = r.stdout.strip()
            if out:
                d = json.loads(out)
                if d.get("InterfaceAlias"):
                    return {"alias": str(d["InterfaceAlias"]), "index": int(d.get("InterfaceIndex") or 0)}
        except Exception:
            pass
        if tries > 1:
            time.sleep(0.5)
    return {"alias": TAP_ADAPTER_NAME, "index": 0}


def _wrong_adapter_hint() -> str:
    """Best guess why edge skipped LanPartyVPN, for the log."""
    hints = []
    st = tap_adapter_state(cache_s=0)
    if st != "ok":
        hints.append(f"LanPartyVPN is {st}")
    try:
        pattern = os.path.join(_BUNDLED_DIR, "*").replace("'", "''")
        r = _ps("Get-CimInstance Win32_Process -Filter \"Name='edge.exe'\" | "
                f"Where-Object {{ $_.ExecutablePath -notlike '{pattern}' }} | "
                "ForEach-Object { $_.ExecutablePath }", timeout=15)
        others = [p for p in r.stdout.strip().splitlines() if p.strip()]
        if others:
            hints.append("another edge.exe is running and may hold LanPartyVPN: " + ", ".join(others[:3]) +
                         " (an older launcher copy? close it)")
    except Exception:
        pass
    if len([a for a in _tap_adapters() if str(a.get("Status")) not in ("Disabled", "Not Present")]) > 1:
        hints.append("this PC has more than one TAP adapter and n2n opened another one")
    return "; ".join(hints) or "cause unknown"


def _ensure_supernode_rule(port: int, log: list) -> None:
    """Allow the supernode's UDP port in from the internet on every profile (home Wi-Fi is often Public).
    Narrow on purpose: this exe, UDP, this one port."""
    name = f"LanParty supernode UDP {port}"
    netsh = _sys_exe("netsh.exe")
    try:
        r = subprocess.run([netsh, "advfirewall", "firewall", "show", "rule", f"name={name}"], capture_output=True,
                           text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if name in r.stdout:
            return
        subprocess.run([netsh, "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow",
                        f"program={os.path.join(_BUNDLED_DIR, 'supernode.exe')}", "protocol=UDP", f"localport={port}",
                        "profile=any"],
                       capture_output=True, text=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.append(f"Firewall: allowed supernode on UDP {port}.")
    except Exception as e:
        log.append(f"Firewall rule for the supernode failed: {e}")


def _ensure_launcher_rules(log: list) -> None:
    """Let party members reach this launcher (TCP 47801 web/peer API, UDP 47800 discovery) over the VPN only:
    this python.exe, those ports, remote addresses on the VPN subnet. PC2 may never have been asked by Windows."""
    netsh = _sys_exe("netsh.exe")
    for proto, port in (("TCP", 47801), ("UDP", 47800)):
        name = f"LanParty launcher VPN {proto} {port}"
        try:
            r = subprocess.run([netsh, "advfirewall", "firewall", "show", "rule", f"name={name}"],
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if name in r.stdout:
                continue
            subprocess.run([netsh, "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
                            "action=allow", f"program={sys.executable}", f"protocol={proto}", f"localport={port}",
                            f"remoteip={VPN_SUBNET}.0/24", "profile=any"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.append(f"Firewall: allowed the launcher on {proto} {port} from the VPN.")
        except Exception as e:
            log.append(f"Firewall rule for the launcher failed: {e}")


# ---- game network: make LanPartyVPN look like the LAN to every game ----
# Games broadcast to 255.255.255.255 from an unbound socket and auto-detect "my IP" with a host name lookup; Windows
# answers both with the lowest-metric adapter (VirtualBox/VMware/Wi-Fi on a typical PC), so VPN players never saw
# each other. While a party is active: LanPartyVPN gets metric 1, and one inbound rule allows everything from the
# VPN subnet (everyone on it holds the party key), so DirectPlay, IPX-over-UDP and each game's own ports work.
# The old metric is saved first, so stop, exit and the next start after a crash put things back.

GAMENET_RULE = "LanParty VPN peers (all games)"
GAMENET_METRIC = 1


def _metric_backup_path(data_dir: str) -> str:
    return os.path.join(_tools_dir(data_dir), "metric_backup.json")


def game_network_active(data_dir: str) -> bool:
    return os.path.isfile(_metric_backup_path(data_dir))


def _netsh_rule(args: list) -> subprocess.CompletedProcess:
    return subprocess.run([_sys_exe("netsh.exe"), "advfirewall", "firewall"] + args, capture_output=True, text=True,
                          timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _delete_gamenet_rule() -> bool:
    """True if the rule is gone afterwards (netsh says 'No rules match' when there was none)."""
    try:
        r = _netsh_rule(["delete", "rule", f"name={GAMENET_RULE}"])
    except Exception:
        return False
    return r.returncode == 0 or "no rules match" in (r.stdout + r.stderr).lower()


def _load_backup(data_dir: str):
    """The backup dict, {} if unreadable, None if there is none."""
    path = _metric_backup_path(data_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_backup(data_dir: str, d: dict, log: list) -> None:
    try:
        with open(_metric_backup_path(data_dir), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(d, fh)
    except OSError as e:
        log.append(f"Could not save the game network backup: {e}")


def game_network_adapter(data_dir: str) -> str:
    """The adapter the game network step changed (from the backup), or ''."""
    d = _load_backup(data_dir) or {}
    return "" if d.get("rules_only") else str(d.get("alias") or "")


def _find_block_rules(exes: list) -> list:
    """Enabled inbound BLOCK rules for these programs: [{name, display, program}]. Windows makes these when
    someone cancels the 'Allow access?' prompt, and a block beats every allow rule (seen: Dark Reign 2's
    announcements never reached the laptop)."""
    exes = sorted({os.path.normpath(e) for e in exes if e})
    if not exes or sys.platform != "win32":
        return []
    lst = ",".join(_q(e) for e in exes)
    r = _ps(f"$exes = @({lst}); Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True "
            "-ErrorAction SilentlyContinue | ForEach-Object { $f = $_ | Get-NetFirewallApplicationFilter; "
            "$p = [Environment]::ExpandEnvironmentVariables([string]$f.Program); if ($exes -contains $p) { "
            "[pscustomobject]@{name=$_.Name; display=$_.DisplayName; program=$p} } } | ConvertTo-Json -Compress",
            timeout=120)
    out = r.stdout.strip()
    if not out:
        return []
    d = json.loads(out)
    return [x for x in (d if isinstance(d, list) else [d]) if isinstance(x, dict) and x.get("name")]


def disable_game_block_rules(data_dir: str, exes: list, log: list) -> int:
    """During a party: disable (never delete) inbound block rules for these game programs and remember them, so
    restore and crash recovery turn exactly those back on. Returns how many were disabled."""
    try:
        found = _find_block_rules(exes)
    except Exception as e:
        log.append(f"Could not check the firewall for game block rules: {e}")
        return 0
    if not found:
        return 0
    saved = _load_backup(data_dir)
    if saved is None:
        saved = {"rules_only": True}
    done = list(saved.get("disabled_rules") or [])
    have = {d.get("name") for d in done}
    n = 0
    for rule in found:
        if rule["name"] in have:
            continue
        try:
            r = _ps(f"Disable-NetFirewallRule -Name {_q(rule['name'])} -ErrorAction Stop; 'ok'")
            if "ok" not in r.stdout:
                raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or "no answer")
        except Exception as e:
            log.append(f"Could not disable block rule '{rule.get('display')}' for {rule.get('program')}: {e}")
            continue
        done.append({"name": rule["name"], "display": rule.get("display", ""), "program": rule.get("program", "")})
        have.add(rule["name"])
        n += 1
        log.append(f"Disabled block rule '{rule.get('display')}' for {rule.get('program')} during the party.")
        saved["disabled_rules"] = done
        _save_backup(data_dir, saved, log)      # after each one, so a crash still knows what to turn back on
    return n


def apply_game_network(data_dir: str, log: list, adapter: dict = None) -> bool:
    """Metric 1 on the adapter that holds our VPN address (normally LanPartyVPN) and the allow-all-from-the-VPN
    rule. Each step logs; a failure doesn't stop the party. Returns True if both steps worked."""
    ok = True
    alias = (adapter or {}).get("alias") or TAP_ADAPTER_NAME
    saved_alias = game_network_adapter(data_dir)
    if saved_alias and saved_alias != alias:   # a backup for another adapter: put that one back first
        restore_game_network(data_dir, log)
    saved = _load_backup(data_dir)
    if saved is None or saved.get("rules_only"):   # keep the first backup: it holds the metric from before the party
        metric, automatic = None, True
        try:
            r = _ps(f"$i = Get-NetIPInterface -InterfaceAlias {_q(alias)} -AddressFamily IPv4 "
                    "-ErrorAction Stop; '' + [int]$i.InterfaceMetric + ',' + [string]$i.AutomaticMetric")
            m, _, auto = r.stdout.strip().partition(",")
            metric, automatic = int(m), auto.strip() != "Disabled"
        except Exception:
            log.append(f"Could not read the {alias} metric; it will be set back to automatic.")
        saved = dict(saved or {})
        saved.pop("rules_only", None)
        saved.update({"alias": alias, "index": (adapter or {}).get("index", 0), "metric": metric,
                      "automatic": automatic, "rule": GAMENET_RULE, "at": time.time()})
        _save_backup(data_dir, saved, log)
    try:
        r = _ps(f"Set-NetIPInterface -InterfaceAlias {_q(alias)} -AddressFamily IPv4 "
                f"-InterfaceMetric {GAMENET_METRIC} -ErrorAction Stop; 'ok'")
        if "ok" not in r.stdout:
            raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or "no answer")
        log.append(f"{alias} metric set to {GAMENET_METRIC}: games' broadcasts and 'my IP' use the VPN.")
    except Exception as e:
        ok = False
        log.append(f"Could not set the {alias} metric (admin needed?): {e}")
    _delete_gamenet_rule()                # never two copies
    try:
        r = _netsh_rule(["add", "rule", f"name={GAMENET_RULE}", "dir=in", "action=allow",
                         f"remoteip={VPN_SUBNET}.0/24", "profile=any"])
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[-200:])
        log.append(f"Firewall: allowed all games from the VPN ({VPN_SUBNET}.0/24) while the party is on.")
    except Exception as e:
        ok = False
        log.append(f"Firewall rule for games on the VPN failed (admin needed?): {e}")
    return ok


def restore_game_network(data_dir: str, log: list) -> bool:
    """Undo apply_game_network if it ran (the backup file exists). The backup is only deleted once both the metric
    and the rule are back, so a non-admin start leaves it for the next admin start. Returns True if nothing is
    left to undo."""
    path = _metric_backup_path(data_dir)
    saved = _load_backup(data_dir)
    if saved is None:
        return True
    ok = True
    for rule in list(saved.get("disabled_rules") or []):
        try:
            r = _ps(f"Enable-NetFirewallRule -Name {_q(rule.get('name'))} -ErrorAction Stop; 'ok'")
            if "ok" in r.stdout:
                log.append(f"Turned block rule '{rule.get('display')}' for {rule.get('program')} back on.")
            elif "no msft_netfirewallrule objects found" in (r.stderr + r.stdout).lower():
                log.append(f"Block rule '{rule.get('display')}' is gone; nothing to turn back on.")
            else:
                raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or "no answer")
        except Exception as e:
            ok = False
            log.append(f"Could not turn block rule '{rule.get('display')}' back on (admin needed?): {e}")
    if saved.get("rules_only"):
        if ok:
            try:
                os.remove(path)
            except OSError:
                pass
        return ok
    metric = saved.get("metric")
    alias = str(saved.get("alias") or TAP_ADAPTER_NAME)
    if saved.get("automatic", True) or not isinstance(metric, int):
        cmd, what = "-AutomaticMetric Enabled", "automatic"
    else:
        cmd, what = f"-InterfaceMetric {metric}", str(metric)
    try:
        r = _ps(f"Set-NetIPInterface -InterfaceAlias {_q(alias)} -AddressFamily IPv4 {cmd} "
                "-ErrorAction Stop; 'ok'")
        if "ok" not in r.stdout:
            raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or "no answer")
        log.append(f"{alias} metric set back to {what}.")
    except Exception as e:
        if "no msft_netipinterface objects found" in str(e).lower():
            log.append(f"{alias} is gone; nothing to set back.")   # adapter removed: don't retry forever
        else:
            ok = False
            log.append(f"Could not set the {alias} metric back to {what} (admin needed?): {e}")
    if _delete_gamenet_rule():
        log.append("Firewall: removed the games-from-the-VPN rule.")
    else:
        ok = False
        log.append(f"Could not remove the firewall rule '{GAMENET_RULE}' (admin needed?).")
    if ok:
        try:
            os.remove(path)
        except OSError:
            pass
    return ok


def recover_game_network(data_dir: str, log: list) -> None:
    """On launcher start: a backup left behind means the last launcher crashed or was closed mid-party."""
    if game_network_active(data_dir):
        log.append("Undoing the game network settings left by the last session...")
        restore_game_network(data_dir, log)


def _wait_registered(log_path: str, timeout: float, proc: subprocess.Popen = None) -> bool:
    """True once edge logs '[OK] edge <<< ... >>> supernode', which it prints on the supernode's REGISTER_SUPER_ACK."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(log_path, "rb") as fh:
                for line in fh.read().decode(errors="replace").splitlines():
                    if "[OK] edge" in line and ">>> supernode" in line:
                        return True
        except OSError:
            pass
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _ip_state(ip: str) -> str:
    """Windows' duplicate-address-detection state for a local IP: Preferred, Tentative, Duplicate, or ''."""
    if sys.platform != "win32":
        return ""
    try:
        r = _ps(f"(Get-NetIPAddress -IPAddress '{ip}' -ErrorAction SilentlyContinue).AddressState", timeout=10)
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    except Exception:
        return ""


def _n2n_env(key: str) -> dict:
    """Pass the key through N2N_KEY so it doesn't show in the process list."""
    env = dict(os.environ)
    env["N2N_KEY"] = key
    return env


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def verify_bundled() -> str:
    """'ok' if both bundled exes match the pinned hashes, else an error string."""
    for name, want in N2N_SHA256.items():
        p = os.path.join(_BUNDLED_DIR, name)
        if not os.path.isfile(p):
            return f"{p} is missing"
        if _sha256_file(p) != want:
            return f"{p} does not match the pinned SHA-256; refusing to run it"
    return "ok"


def download_n2n(data_dir: str, log: list) -> str:
    """n2n ships with the launcher (no official Windows download exists). Returns 'ok' or an error string."""
    if n2n_bundled():
        r = verify_bundled()
        if r == "ok":
            log.append(f"n2n bundled ({N2N_VERSION}), hashes OK.")
        return r
    return (f"n2n is missing. Put edge.exe and supernode.exe ({N2N_VERSION}, windows x64 static) in "
            f"{_BUNDLED_DIR}.")


# ---- process management ----

class VPN:
    """Manages the n2n supernode and edge processes for one party."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._supernode: subprocess.Popen = None
        self._edge: subprocess.Popen = None
        self._lock = threading.Lock()
        self.log: list = []
        self.state: str = "off"        # off, starting, host, joined, error
        self.error: str = ""
        self.my_ip: str = ""           # our IP on the virtual subnet
        self.party_id: str = ""
        self.party_secret: bytes = b""
        self.host_ip: str = ""         # the supernode's public IP
        self.supernode_port: int = SUPERNODE_PORT
        self.party_code: str = ""      # host only; the status API is loopback-only (/api/*)
        self.adapter: str = ""         # adapter that holds my_ip (normally LanPartyVPN)
        self.game_exes = lambda: []    # set by the app: full paths of the library's game programs

    def status(self) -> dict:
        alive_sn = self._supernode is not None and self._supernode.poll() is None
        alive_edge = self._edge is not None and self._edge.poll() is None
        return {"state": self.state, "error": self.error, "my_ip": self.my_ip,
                "supernode": alive_sn, "edge": alive_edge,
                "party_id": self.party_id, "host_ip": self.host_ip, "code": self.party_code,
                "supernode_port": self.supernode_port, "admin": is_admin(),
                "n2n_ready": n2n_ready(self.data_dir), "tap_installed": tap_installed(),
                "tap_adapter": tap_adapter_state(),
                "game_network": game_network_active(self.data_dir),
                "vpn_adapter": self.adapter or game_network_adapter(self.data_dir),
                "vpn_adapter_ok": (self.adapter or TAP_ADAPTER_NAME) == TAP_ADAPTER_NAME,
                "disabled_block_rules": [r.get("display") or r.get("name")
                                         for r in ((_load_backup(self.data_dir) or {}).get("disabled_rules") or [])],
                "log": self.log[-20:]}

    def snapshot(self) -> dict:
        """What the next launcher needs to take this party over (handoff.py). The secret goes in hex; the file is
        DPAPI-encrypted."""
        from . import handoff
        procs = {}
        for name, proc in (("edge", self._edge), ("supernode", self._supernode)):
            if proc is not None and proc.poll() is None:
                procs[name] = handoff.process_identity(proc.pid)
        return {"state": self.state, "my_ip": self.my_ip, "party_id": self.party_id,
                "secret": self.party_secret.hex(), "host_ip": self.host_ip, "supernode_port": self.supernode_port,
                "code": self.party_code, "adapter": self.adapter, "log": self.log[-20:], "procs": procs}

    def resume(self, d: dict, exes: dict = None) -> str:
        """Take over a party handed over by the launcher this update replaced. 'ok' or why not (then nothing is
        taken over; the caller cleans up)."""
        from . import handoff
        if self.state not in ("off", "error"):
            return "already in a party"
        exes = exes or {"edge": _edge_path(self.data_dir), "supernode": _supernode_path(self.data_dir)}
        try:
            state = str(d["state"])
            secret = bytes.fromhex(str(d["secret"]))
            procs = d.get("procs") or {}
        except (KeyError, TypeError, ValueError):
            return "hand-over file is incomplete"
        if state not in ("host", "joined") or len(secret) != 32 or party_id_for(secret) != d.get("party_id"):
            return "hand-over file doesn't describe a party"
        need = ["edge"] + (["supernode"] if state == "host" else [])
        got = {}
        for name in need:
            p = handoff.adopt(procs.get(name), exes[name])
            if p is None:
                for q in got.values():
                    try:
                        import ctypes
                        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(q._handle))
                    except Exception:
                        pass
                return f"n2n {name} from before the update isn't running any more"
            got[name] = p
        for p in got.values():
            _kill_on_exit(p)        # tie them to this launcher, as if it had started them
        self._edge, self._supernode = got.get("edge"), got.get("supernode")
        self.state, self.error = state, ""
        self.my_ip = str(d.get("my_ip") or "")
        self.party_secret, self.party_id = secret, party_id_for(secret)
        self.host_ip = str(d.get("host_ip") or "")
        self.supernode_port = int(d.get("supernode_port") or SUPERNODE_PORT)
        self.party_code = str(d.get("code") or "") if state == "host" else ""
        self.adapter = str(d.get("adapter") or "")
        self.log = [str(x) for x in (d.get("log") or [])][-19:] + ["Launcher updated; still connected to the party."]
        return "ok"

    def create_party(self, external_ip: str = "", port: int = SUPERNODE_PORT, secret: bytes = None) -> str:
        """Start a new party as host. Returns the party code or an error string. With the secret of an earlier
        party ('Host again'), it's the same party: same id, key and, if the IPs didn't change, the same code."""
        if self.state not in ("off", "error"):
            return "already in a party"
        self.state = "starting"
        self.error = ""
        self.log = []

        if not is_admin():
            self.state = "error"
            self.error = ADMIN_NEEDED
            return self.error
        if True:   # always re-check the pinned hashes before starting
            r = download_n2n(self.data_dir, self.log)
            if r != "ok":
                self.state = "error"
                self.error = r
                return r

        kill_stale_n2n(self.log)
        r = ensure_tap_adapter(self.data_dir, self.log)
        if r != "ok":
            self.state = "error"
            self.error = r
            return self.error

        # host: joiners' own (path-measured) MTUs cap what the host sends them over TCP via their MSS
        _set_tap_mtu(self.log, MTU_HOST)

        self.party_secret = secret if isinstance(secret, bytes) and len(secret) == 32 else os.urandom(32)
        self.party_id = party_id_for(self.party_secret)
        self.host_ip = external_ip or detect_external_ip()
        self.supernode_port = port
        self.my_ip = f"{VPN_SUBNET}.1"

        if not self.host_ip:
            self.state = "error"
            self.error = "Could not detect your external IP. Enter it manually."
            return self.error

        community = COMMUNITY_PREFIX + self.party_id
        key = n2n_key_for(self.party_secret)

        # start supernode
        sn = _supernode_path(self.data_dir)
        sn_cmd = [sn, "-l", str(self.supernode_port)]
        _ensure_supernode_rule(self.supernode_port, self.log)
        self.log.append(f"Starting supernode on UDP {self.supernode_port}...")
        sn_log = os.path.join(_tools_dir(self.data_dir), "supernode.log")
        try:
            self._supernode = _spawn(sn_cmd, sn_log)
        except Exception as e:
            self.state = "error"
            self.error = f"supernode start failed: {e}"
            return self.error
        time.sleep(0.5)
        if self._supernode.poll() is not None:
            self.state = "error"
            self.error = f"supernode exited: {_log_tail(sn_log, 200)}"
            return self.error

        # start edge
        edge = _edge_path(self.data_dir)
        edge_cmd = [edge, "-c", community, "-a", self.my_ip,
                    "-l", f"127.0.0.1:{self.supernode_port}",
                    N2N_CIPHER,         # ChaCha20 (key via N2N_KEY)
                    "-E",               # multicast/broadcast
                    "-r",               # enable routing
                    "-d", TAP_ADAPTER_NAME]   # our own adapter only
        self.log.append("Starting edge (host)...")
        edge_log = os.path.join(_tools_dir(self.data_dir), "edge.log")
        try:
            self._edge = _spawn(edge_cmd, edge_log, _n2n_env(key))
        except Exception as e:
            self.stop()
            self.state = "error"
            self.error = f"edge start failed: {e}"
            return self.error
        time.sleep(1)
        if self._edge.poll() is not None:
            err = _log_tail(edge_log)
            self.stop()
            self.state = "error"
            self.error = f"edge exited: {err[:200]}"
            return self.error

        self._game_network(MTU_HOST)
        self.state = "host"
        lan = host_lan_ips(self.host_ip)
        if lan:
            self.log.append(f"Players on your LAN connect to {', '.join(lan)}:{self.supernode_port}; "
                            f"the others to {self.host_ip}:{self.supernode_port}.")
        code = encode_party_code(self.host_ip, self.supernode_port, self.party_secret, lan)
        self.party_code = code
        self.log.append("Party created. Share the party code shown above.")   # keep the secret out of the log
        return code

    def join_party(self, code: str) -> str:
        """Join an existing party by code. Returns 'ok' or an error."""
        if self.state not in ("off", "error"):
            return "already in a party"
        self.state = "starting"
        self.error = ""
        self.log = []

        try:
            host_ip, port, secret, host_lan = decode_party_code_full(code)
        except (ValueError, Exception) as e:
            self.state = "error"
            self.error = f"Bad party code: {e}"
            return self.error

        if not is_admin():
            self.state = "error"
            self.error = ADMIN_NEEDED
            return self.error
        if True:   # always re-check the pinned hashes before starting
            r = download_n2n(self.data_dir, self.log)
            if r != "ok":
                self.state = "error"
                self.error = r
                return r

        kill_stale_n2n(self.log)
        r = ensure_tap_adapter(self.data_dir, self.log)
        if r != "ok":
            self.state = "error"
            self.error = r
            return self.error

        from .discovery import local_ipv4s
        pid = party_id_for(secret)
        sns = supernode_order(host_ip, host_lan, local_ipv4s(), lambda ip: probe_lan_host(ip, pid))
        if sns[0] != host_ip:
            self.log.append(f"The host is on your LAN: connecting to {sns[0]} first.")
        pm = path_mtu(sns[0])
        mtu = vpn_mtu_for(pm)
        self.log.append(f"Path to the host carries {pm}-byte packets." if pm else
                        "The host doesn't answer pings; using the safe packet size.")
        _set_tap_mtu(self.log, mtu)

        self.party_secret = secret
        self.party_id = party_id_for(secret)
        self.host_ip = host_ip
        self.supernode_port = port

        community = COMMUNITY_PREFIX + self.party_id
        key = n2n_key_for(secret)
        edge = _edge_path(self.data_dir)
        edge_log = os.path.join(_tools_dir(self.data_dir), "edge.log")
        tried = {1}                   # .1 is always the host
        self.log.append(f"Connecting to {' / '.join(f'{h}:{port}' for h in sns)}...")
        sn_args = []
        for h in sns:
            sn_args += ["-l", f"{h}:{port}"]

        # Random address .2-.254. If another player already has it, Windows' duplicate-address detection marks it
        # Duplicate once n2n configures the adapter (ARP crosses the tunnel), or the n2n6 supernode logs a conflict;
        # then stop and try another address.
        for attempt in range(JOIN_IP_TRIES):
            n = 2 + int.from_bytes(os.urandom(2), "big") % 253
            while n in tried:
                n = 2 + int.from_bytes(os.urandom(2), "big") % 253
            tried.add(n)
            self.my_ip = f"{VPN_SUBNET}.{n}"
            edge_cmd = [edge, "-c", community, "-a", self.my_ip, *sn_args,
                        N2N_CIPHER, "-E", "-r", "-d", TAP_ADAPTER_NAME]
            try:
                self._edge = _spawn(edge_cmd, edge_log, _n2n_env(key))
            except Exception as e:
                self.state = "error"
                self.error = f"edge start failed: {e}"
                return self.error
            time.sleep(2)
            if self._edge.poll() is not None:
                self._edge = None
                self.state = "error"
                self.error = f"edge exited: {_log_tail(edge_log, 200)}"
                return self.error
            # A running edge isn't a connected one: wait for the supernode's answer.
            if not _wait_registered(edge_log, JOIN_REGISTER_WAIT * len(sns), self._edge):   # time to fail over
                self._kill_edge()
                self.state = "error"
                self.error = (f"Can't reach the host's supernode at {host_ip}:{port}. The host's UDP port {port} "
                              "isn't reachable from the internet: it needs forwarding on the host's router, and a "
                              "host on phone tethering or mobile data can't be reached at all (carrier NAT). "
                              "Have someone on home internet with the port forwarded host instead.")
                return self.error
            st = ""
            for _ in range(16):       # up to ~8 s for registration + duplicate-address detection
                st = _ip_state(self.my_ip)
                if st in ("Preferred", "Duplicate"):
                    break
                time.sleep(0.5)
            if st == "Duplicate" or "conflict" in _log_tail(edge_log, 2000).lower():
                self.log.append(f"{self.my_ip} is taken by another player, trying another address...")
                self._kill_edge()
                continue
            self._game_network(mtu)
            self.state = "joined"
            self.log.append(f"Connected. Your VPN IP: {self.my_ip}")
            return "ok"

        self.state = "error"
        self.error = f"No free VPN address after {JOIN_IP_TRIES} tries."
        return self.error

    def _game_network(self, mtu: int) -> None:
        """Per-adapter settings go to the adapter that really holds our VPN address."""
        ad = vpn_adapter_for(self.my_ip)
        self.adapter = ad["alias"]
        if ad["alias"] != TAP_ADAPTER_NAME:
            self.log.append(f"Warning: n2n is using '{ad['alias']}' instead of {TAP_ADAPTER_NAME}; its settings go "
                            f"to '{ad['alias']}'. Likely cause: {_wrong_adapter_hint()}.")
            _set_tap_mtu(self.log, mtu, ad["alias"])
        _set_vpn_private(self.log, ad["alias"])
        _ensure_launcher_rules(self.log)
        apply_game_network(self.data_dir, self.log, ad)
        try:
            exes = list(self.game_exes())
        except Exception:
            exes = []
        disable_game_block_rules(self.data_dir, exes, self.log)

    def open_blocked_for(self, exes: list) -> int:
        """A game started during the party: make sure no block rule stops it hearing the others."""
        if not self.is_active():
            return 0
        return disable_game_block_rules(self.data_dir, exes, self.log)

    def _kill_edge(self) -> None:
        with self._lock:
            proc, self._edge = self._edge, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def stop(self) -> None:
        """Shut down all VPN processes."""
        with self._lock:
            for proc in (self._edge, self._supernode):
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            self._edge = None
            self._supernode = None
        restore_game_network(self.data_dir, self.log)
        self.adapter = ""
        self.state = "off"
        self.my_ip = ""
        self.party_id = ""
        self.party_secret = b""
        self.party_code = ""
        self.log.append("VPN stopped.")

    def is_active(self) -> bool:
        return self.state in ("host", "joined")
