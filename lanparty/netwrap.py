"""Makes old network games see each other on a modern LAN.

- Windows IPX/SPX games: IPXWrapper DLLs next to the game exe turn IPX into UDP broadcast on the LAN.
- DOS games: DOSBox's built-in IPX-over-UDP; the party host runs an ipxnet server, everyone else connects.
- Windows Firewall allow rules, and a check for virtual adapters that can swallow game broadcasts."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from . import DISCOVERY_PORT, HTTP_PORT
from .prereqs import run_elevated

IPXW_API = "https://api.github.com/repos/solemnwarning/ipxwrapper/releases/latest"
IPXW_FILES = ["ipxwrapper.dll", "wsock32.dll", "mswsock.dll", "dpwsockx.dll", "ipxconfig.exe"]
IPXW_PORT = 54792
DOSBOX_IPX_PORT = 21300
MARKER = ".lanparty-ipxwrapper.json"
VIRTUAL_NIC = re.compile(r"(virtualbox|vmware|hyper-v|vethernet|tap-|tap |wireguard|zerotier|hamachi|radmin|"
                         r"tailscale|vpn|loopback|wsl|docker|npcap)", re.I)

# DOSBox-X 2026.10.01 (vsbuild win64 SDL2, portable) ships with the launcher, so DOS games need no install.
BUNDLED_DOSBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "dosbox-x", "dosbox-x.exe")
DOSBOX_GUESSES = [
    r"C:\Program Files (x86)\DOSBox-0.74-3\DOSBox.exe",
    r"C:\Program Files (x86)\DOSBox-0.74\DOSBox.exe",
    r"C:\Program Files\DOSBox Staging\dosbox.exe",
    r"C:\Program Files\DOSBox-X\dosbox-x.exe",
    r"C:\DOSBox-X\dosbox-x.exe",
]


_NETSH_OK = re.compile(r'^netsh(\.exe)? advfirewall firewall (add|delete|set) rule [^&|<>^%!\r\n`$;]*$', re.I)


def safe_netsh(cmd: str) -> bool:
    """lan.json firewall lines run as admin, so only plain 'netsh advfirewall firewall ... rule' commands pass."""
    return bool(_NETSH_OK.match(cmd.strip()))


class NetWrap:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tools = cfg.sub("tools")
        self.ipxw_dir = os.path.join(self.tools, "ipxwrapper")
        self.log = []

    def _log(self, m: str) -> None:
        self.log = (self.log + [m])[-100:]

    # ---- IPXWrapper ----
    def ipxwrapper_ready(self) -> bool:
        return all(os.path.exists(os.path.join(self.ipxw_dir, f)) for f in IPXW_FILES[:3])

    def fetch_ipxwrapper(self) -> str:
        """Downloads the latest IPXWrapper release from its GitHub page."""
        try:
            req = urllib.request.Request(IPXW_API, headers={"User-Agent": "LanPartyLauncher",
                                                            "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rel = json.loads(r.read().decode("utf-8"))
            asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")
                          and "src" not in a["name"].lower() and "source" not in a["name"].lower()), None)
            if not asset:
                return "No zip found in the latest IPXWrapper release."
            with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as r:
                data = r.read()
            os.makedirs(self.ipxw_dir, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    base = os.path.basename(name).lower()
                    if base in IPXW_FILES or base.endswith((".txt", ".md")):
                        with z.open(name) as src, open(os.path.join(self.ipxw_dir, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            self._log(f"IPXWrapper {rel.get('tag_name', '')} ready in {self.ipxw_dir}")
            return "ok" if self.ipxwrapper_ready() else "Downloaded, but the DLLs were not in the zip."
        except Exception as e:  # noqa: BLE001
            return f"IPXWrapper download failed: {e}"

    def wrapper_installed(self, game: dict) -> bool:
        return os.path.exists(os.path.join(self._exe_dir(game), MARKER))

    @staticmethod
    def _exe_dir(game: dict) -> str:
        return os.path.dirname(os.path.join(game["path"], game["exe"])) if game.get("exe") else game["path"]

    def install_wrapper(self, game: dict) -> str:
        if game.get("arch") == "x64":
            return "IPXWrapper only works with 32-bit games."
        if not self.ipxwrapper_ready():
            r = self.fetch_ipxwrapper()
            if r != "ok":
                return r
        d = self._exe_dir(game)
        backed = []
        for f in IPXW_FILES:
            src = os.path.join(self.ipxw_dir, f)
            if not os.path.exists(src):
                continue
            dst = os.path.join(d, f)
            if os.path.exists(dst) and not os.path.exists(os.path.join(d, MARKER)):
                shutil.copy2(dst, dst + ".lanparty-bak")
                backed.append(f)
            shutil.copy2(src, dst)
        with open(os.path.join(d, MARKER), "w", encoding="utf-8") as fh:
            json.dump({"backed_up": backed, "files": IPXW_FILES}, fh)
        self._log(f"IPXWrapper installed for {game['title']}")
        return "ok"

    def remove_wrapper(self, game: dict) -> str:
        d = self._exe_dir(game)
        mp = os.path.join(d, MARKER)
        if not os.path.exists(mp):
            return "not installed"
        with open(mp, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        for f in meta.get("files", []):
            p = os.path.join(d, f)
            if os.path.exists(p):
                os.remove(p)
            if f in meta.get("backed_up", []) and os.path.exists(p + ".lanparty-bak"):
                os.replace(p + ".lanparty-bak", p)
        os.remove(mp)
        return "ok"

    # ---- DOSBox ----
    def dosbox_path(self) -> str:
        p = self.cfg.get("dosbox_path") or ""
        if p and os.path.exists(p):
            return p                    # set by hand in Settings: the player's choice wins
        if os.path.isfile(BUNDLED_DOSBOX):
            return BUNDLED_DOSBOX
        bundled = os.path.join(self.tools, "dosbox")
        if os.path.isdir(bundled):
            for fn in os.listdir(bundled):
                if fn.lower() in ("dosbox.exe", "dosbox-x.exe"):
                    return os.path.join(bundled, fn)
        for g in DOSBOX_GUESSES:
            if os.path.exists(g):
                return g
        for name in ("dosbox", "dosbox-x"):
            w = shutil.which(name)
            if w:
                return w
        return ""

    @staticmethod
    def dosbox_argv(db: str, conf: str) -> list:
        """Command line for a DOSBox. DOSBox-X: -fastlaunch skips its splash screen, -nopromptfolder stops it
        asking on first start where to keep its files."""
        argv = [db]
        if os.path.basename(db).lower().startswith("dosbox-x"):
            argv += ["-fastlaunch", "-nopromptfolder"]
        return argv + ["-conf", conf]

    def dosbox_conf(self, game: dict, role: str, host_ip: str, args: str) -> str:
        exe = game.get("exe") or ""
        exe_dir, exe_name = os.path.split(exe.replace("/", "\\"))
        lines = ["[ipx]", "ipx=true", "", "[autoexec]", f'mount c "{game["path"]}"', "c:"]
        if exe_dir:
            lines.append(f"cd \\{exe_dir}")
        if role == "host":
            lines.append(f"ipxnet startserver {DOSBOX_IPX_PORT}")
        elif role == "join" and host_ip:
            lines.append(f"ipxnet connect {host_ip} {DOSBOX_IPX_PORT}")
        lines.append(f"{exe_name} {args}".strip())
        lines.append("exit")
        path = os.path.join(self.cfg.sub("cache", "dosbox"), f"{game['id']}_{role}.conf")
        with open(path, "w", encoding="ascii", errors="replace") as fh:
            fh.write("\r\n".join(lines) + "\r\n")
        return path

    # ---- firewall + adapters ----
    def firewall_lines(self, games: list) -> list:
        lines = []

        def q(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        def rule(name: str, extra: str) -> None:
            lines.append(f"Remove-NetFirewallRule -DisplayName {q(name)} -ErrorAction SilentlyContinue")
            lines.append(f"New-NetFirewallRule -DisplayName {q(name)} -Direction Inbound -Action Allow -Profile Any {extra} | Out-Null")

        rule("LanParty launcher", f"-Protocol TCP -LocalPort {HTTP_PORT}")
        rule("LanParty discovery", f"-Protocol UDP -LocalPort {DISCOVERY_PORT}")
        rule("LanParty IPXWrapper", f"-Protocol UDP -LocalPort {IPXW_PORT}")
        rule("LanParty DOSBox IPX", f"-Protocol UDP -LocalPort {DOSBOX_IPX_PORT}")
        rule("LanParty python", f"-Program {q(sys.executable)}")
        db = self.dosbox_path()
        if db:
            rule("LanParty DOSBox", f"-Program {q(db)}")
        custom = []
        for g in games:
            if g.get("exe") and g["exe"].lower().endswith(".exe") and g.get("kind") == "pe":
                rule(f"LanParty game {g['title']}", f"-Program {q(os.path.join(g['path'], g['exe']))}")
            lan = g.get("lan") or {}
            for proto, ports in (lan.get("ports") or {}).items():
                plist = ",".join(q(p.strip()) for p in ports.split(",") if p.strip())
                rule(f"LanParty game {g['title']} {proto.upper()}", f"-Protocol {proto.upper()} -LocalPort {plist}")
            custom += [c for c in lan.get("firewall", []) if safe_netsh(c)]
        if custom:
            cmd_file = os.path.join(self.cfg.sub("cache"), "lan_json_firewall.cmd")
            with open(cmd_file, "w", encoding="ascii", errors="replace") as fh:
                fh.write("@echo off\r\n" + "\r\n".join(custom) + "\r\n")
            lines.append(f"& cmd.exe /c {q(cmd_file)}")
        lines.append("Write-Output 'Firewall rules added.'")
        return lines

    def open_firewall(self, games: list, log) -> bool:
        """True if the rules were added (the script got to its last line)."""
        added = []
        rc = run_elevated(self.cfg, self.firewall_lines(games),
                          lambda m: (added.append(1) if m == "Firewall rules added." else None, log(m)))
        return rc == 0 and bool(added)

    @staticmethod
    def adapters() -> list:
        """[{name, ipv4, virtual}] from ipconfig."""
        if sys.platform != "win32":
            return []
        try:
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=10,
                                 creationflags=0x08000000).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        res = []
        cur = None
        for line in out.splitlines():
            if line and not line.startswith(" ") and line.rstrip().endswith(":"):
                cur = {"name": line.strip().rstrip(":"), "ipv4": "", "virtual": False}
                cur["virtual"] = bool(VIRTUAL_NIC.search(cur["name"]))
                res.append(cur)
            elif cur is not None and "IPv4" in line and ":" in line:
                cur["ipv4"] = line.split(":", 1)[1].strip()
        return [a for a in res if a["ipv4"]]
