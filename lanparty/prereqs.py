"""Known prerequisites: work out which ones a game needs, check whether this PC has them, install them.

All installs run in one elevated PowerShell script, so the player sees a single UAC prompt."""
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import winreg

WINDIR = os.environ.get("WINDIR", r"C:\Windows")
SYS32 = os.path.join(WINDIR, "System32")
SYSWOW = os.path.join(WINDIR, "SysWOW64")

DX_URL = "https://download.microsoft.com/download/8/4/A/84A35BF1-DAFE-4AE8-82AF-AD2AE20B6B14/directx_Jun2010_redist.exe"

# id -> name, how to install. kind: exe (run with args), msi, dism (Windows feature), dxredist, page (open a web page).
CATALOG = {
    "directx_jun2010": {"name": "DirectX 9.0c runtime (June 2010)", "kind": "dxredist", "url": DX_URL},
    "vc2005_x86": {"name": "Visual C++ 2005 (x86)", "kind": "exe", "args": "/q",
                   "url": "https://download.microsoft.com/download/8/B/4/8B42259F-5D70-43F4-AC2E-4B208FD8D66A/vcredist_x86.EXE"},
    "vc2008_x86": {"name": "Visual C++ 2008 (x86)", "kind": "exe", "args": "/q",
                   "url": "https://download.microsoft.com/download/5/D/8/5D8C65CB-C849-4025-8E95-C3966CAFD8AE/vcredist_x86.exe"},
    "vc2010_x86": {"name": "Visual C++ 2010 (x86)", "kind": "exe", "args": "/q /norestart",
                   "url": "https://download.microsoft.com/download/1/6/5/165255E7-1014-4D0A-B094-B6A430A6BFFC/vcredist_x86.exe"},
    "vc2010_x64": {"name": "Visual C++ 2010 (x64)", "kind": "exe", "args": "/q /norestart",
                   "url": "https://download.microsoft.com/download/1/6/5/165255E7-1014-4D0A-B094-B6A430A6BFFC/vcredist_x64.exe"},
    "vc2012_x86": {"name": "Visual C++ 2012 (x86)", "kind": "exe", "args": "/install /quiet /norestart",
                   "url": "https://download.microsoft.com/download/1/6/B/16B06F60-3B20-4FF2-B699-5E9B7962F9AE/VSU_4/vcredist_x86.exe"},
    "vc2012_x64": {"name": "Visual C++ 2012 (x64)", "kind": "exe", "args": "/install /quiet /norestart",
                   "url": "https://download.microsoft.com/download/1/6/B/16B06F60-3B20-4FF2-B699-5E9B7962F9AE/VSU_4/vcredist_x64.exe"},
    "vc2013_x86": {"name": "Visual C++ 2013 (x86)", "kind": "exe", "args": "/install /quiet /norestart",
                   "url": "https://aka.ms/highdpimfc2013x86enu"},
    "vc2013_x64": {"name": "Visual C++ 2013 (x64)", "kind": "exe", "args": "/install /quiet /norestart",
                   "url": "https://aka.ms/highdpimfc2013x64enu"},
    "vc2015plus_x86": {"name": "Visual C++ 2015-2022 (x86)", "kind": "exe", "args": "/install /quiet /norestart",
                       "url": "https://aka.ms/vs/17/release/vc_redist.x86.exe"},
    "vc2015plus_x64": {"name": "Visual C++ 2015-2022 (x64)", "kind": "exe", "args": "/install /quiet /norestart",
                       "url": "https://aka.ms/vs/17/release/vc_redist.x64.exe"},
    "netfx35": {"name": ".NET Framework 3.5 (includes 2.0)", "kind": "dism", "feature": "NetFx3"},
    "netfx48": {"name": ".NET Framework 4.8", "kind": "exe", "args": "/q /norestart",
                "url": "https://go.microsoft.com/fwlink/?linkid=2088631"},
    "directplay": {"name": "DirectPlay (Windows legacy component)", "kind": "dism", "feature": "DirectPlay"},
    "xna40": {"name": "XNA Framework 4.0", "kind": "msi",
              "url": "https://download.microsoft.com/download/A/C/2/AC2C903B-E6E8-42C2-9FD7-BEBAC362A930/xnafx40_redist.msi"},
    "openal": {"name": "OpenAL", "kind": "exe", "args": "/s", "page": "https://www.openal.org/downloads/"},
    "physx_legacy": {"name": "PhysX legacy runtime", "kind": "exe", "args": "/quiet",
                     "page": "https://www.nvidia.com/en-us/drivers/physx/physx-9-13-0604-legacy-driver/"},
}

_DX_DLL = re.compile(r"^(d3dx9_\d+|d3dx10(_\d+)?|d3dx11_\d+|xinput1_[123]|x3daudio1_\d+|xaudio2_[0-7]|xactengine\d_\d+|d3dcompiler_(3\d|4[0-3]))\.dll$")
_VC = {"msvcr80": "vc2005", "msvcp80": "vc2005", "msvcr90": "vc2008", "msvcp90": "vc2008",
       "msvcr100": "vc2010", "msvcp100": "vc2010", "msvcr110": "vc2012", "msvcp110": "vc2012",
       "msvcr120": "vc2013", "msvcp120": "vc2013", "vcruntime140": "vc2015plus", "msvcp140": "vc2015plus",
       "vcruntime140_1": "vc2015plus"}


def needs_from_imports(imports, arch: str, clr, local_files) -> set:
    """Prereq ids implied by an exe's imports. DLLs the game ships next to the exe don't count."""
    out = set()
    arch = arch or "x86"
    for dll in imports:
        if dll in local_files:
            continue
        base = dll[:-4] if dll.endswith(".dll") else dll
        if _DX_DLL.match(dll):
            out.add("directx_jun2010")
        elif base in _VC:
            vc = _VC[base]
            out.add(f"{vc}_x86" if vc in ("vc2005", "vc2008") else f"{vc}_{arch}")
        elif base in ("dplayx", "dpnet"):
            out.add("directplay")
        elif base == "openal32":
            out.add("openal")
        elif base in ("physxloader", "physxcore"):
            out.add("physx_legacy")
        elif base == "mscoree":
            out.add("netfx35" if clr and clr.startswith("v2") else "netfx48")
    if clr and "mscoree.dll" not in imports:
        out.add("netfx35" if clr.startswith("v2") else "netfx48")
    return out


def classify_redist(rel_path: str):
    """Which prereq a redistributable installer shipped inside a game folder provides, if any."""
    p = rel_path.lower().replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if name == "dxsetup.exe" or (("directx" in p) and name.startswith("directx") and name.endswith(".exe")):
        return "directx_jun2010"
    if name.startswith("vcredist") or name.startswith("vc_redist"):
        arch = "x64" if "x64" in p or "amd64" in p else "x86"
        for year, vc in (("2005", "vc2005"), ("2008", "vc2008"), ("2010", "vc2010"), ("2012", "vc2012"),
                         ("2013", "vc2013"), ("2015", "vc2015plus"), ("2017", "vc2015plus"),
                         ("2019", "vc2015plus"), ("2022", "vc2015plus")):
            if year in p:
                return f"{vc}_x86" if vc in ("vc2005", "vc2008") else f"{vc}_{arch}"
        return None
    if "dotnetfx35" in name or "netfx35" in name:
        return "netfx35"
    if name.startswith("ndp48") or "dotnetfx48" in name:
        return "netfx48"
    if name.startswith("oalinst"):
        return "openal"
    if name.startswith("xnafx40"):
        return "xna40"
    if "physx" in name and name.endswith((".exe", ".msi")):
        return "physx_legacy"
    return None


# ---------- checks ----------

def _uninstall_names() -> list:
    names = []
    if not IS_WIN:
        return names
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                k = winreg.OpenKey(root, sub)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    child = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(k, child) as ck:
                        names.append(str(winreg.QueryValueEx(ck, "DisplayName")[0]))
                except OSError:
                    pass
    return names


def _reg_value(path: str, value: str):
    if not IS_WIN:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return winreg.QueryValueEx(k, value)[0]
    except OSError:
        return None


def _sysfile(name: str, wow: bool = True) -> bool:
    d = SYSWOW if (wow and os.path.isdir(SYSWOW)) else SYS32
    return os.path.exists(os.path.join(d, name))


def check_all() -> dict:
    """prereq id -> True/False for this PC."""
    names = [n.lower() for n in _uninstall_names()]

    def vc(year: str, arch: str) -> bool:
        for n in names:
            if "visual c++" in n and year in n and "redistributable" in n:
                is64 = "x64" in n or "amd64" in n
                if (arch == "x64") == is64:
                    return True
        return False

    res = {
        "directx_jun2010": _sysfile("d3dx9_43.dll") and _sysfile("xinput1_3.dll"),
        "vc2005_x86": vc("2005", "x86"),
        "vc2008_x86": vc("2008", "x86"),
        "vc2010_x86": vc("2010", "x86") or _sysfile("msvcr100.dll"),
        "vc2010_x64": vc("2010", "x64") or _sysfile("msvcr100.dll", wow=False),
        "vc2012_x86": vc("2012", "x86") or _sysfile("msvcr110.dll"),
        "vc2012_x64": vc("2012", "x64") or _sysfile("msvcr110.dll", wow=False),
        "vc2013_x86": vc("2013", "x86") or _sysfile("msvcr120.dll"),
        "vc2013_x64": vc("2013", "x64") or _sysfile("msvcr120.dll", wow=False),
        "vc2015plus_x86": _reg_value(r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x86", "Installed") == 1
                          or _reg_value(r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86", "Installed") == 1,
        "vc2015plus_x64": _reg_value(r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64", "Installed") == 1,
        "netfx35": _reg_value(r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v3.5", "Install") == 1,
        "netfx48": (_reg_value(r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release") or 0) >= 528040,
        # dpnet.dll only appears once the DirectPlay feature is on (heuristic).
        "directplay": _sysfile("dpnet.dll"),
        "xna40": _reg_value(r"SOFTWARE\WOW6432Node\Microsoft\XNA\Framework\v4.0", "Installed") == 1
                 or _reg_value(r"SOFTWARE\Microsoft\XNA\Framework\v4.0", "Installed") == 1,
        "openal": _sysfile("OpenAL32.dll"),
        "physx_legacy": _sysfile("PhysXLoader.dll"),
    }
    return res


# ---------- installing ----------

class Installer:
    """Downloads what it can without elevation, then runs one elevated PowerShell script and tails its log."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.status = {"running": False, "log": [], "done": None}
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self.status["log"].append(time.strftime("%H:%M:%S ") + msg)
            self.status["log"] = self.status["log"][-200:]

    def start(self, items: list) -> bool:
        """items: [{id, local: path-or-None}]."""
        if self.status["running"]:
            return False
        self.status = {"running": True, "log": [], "done": None}
        threading.Thread(target=self._run, args=(items,), daemon=True).start()
        return True

    def _download(self, url: str, dest: str) -> bool:
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True
        self.log(f"Downloading {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LanPartyLauncher"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(dest + ".part", dest)
            return True
        except Exception as e:  # noqa: BLE001 - report any network failure to the UI
            self.log(f"Download failed: {e}")
            return False

    def _run(self, items: list) -> None:
        cache = self.cfg.sub("cache", "redist")
        lines = ["$ErrorActionPreference = 'Continue'"]
        pages = []
        for it in items:
            pid = it["id"]
            spec = CATALOG.get(pid)
            if not spec:
                continue
            local = it.get("local")
            kind = spec["kind"]
            if kind == "dism":
                lines.append(f"Write-Output 'Enabling {spec['name']}'")
                lines.append(f"dism.exe /Online /Enable-Feature /FeatureName:{spec['feature']} /All /NoRestart /Quiet")
                continue
            path = local
            if not path and spec.get("url"):
                ext = ".msi" if kind == "msi" else ".exe"
                path = os.path.join(cache, pid + ext)
                if not self._download(spec["url"], path):
                    path = None
            if not path:
                if spec.get("page"):
                    pages.append(spec["page"])
                    self.log(f"{spec['name']}: no bundled installer; opening its download page")
                continue
            q = path.replace("'", "''")
            lines.append(f"Write-Output 'Installing {spec['name']}'")
            if kind == "dxredist" and os.path.basename(path).lower() == "dxsetup.exe":
                lines.append(f"Start-Process -Wait -FilePath '{q}' -ArgumentList '/silent'")
            elif kind == "dxredist":
                tmp = os.path.join(cache, "dx_extract").replace("'", "''")
                lines.append(f"New-Item -ItemType Directory -Force '{tmp}' | Out-Null")
                lines.append(f"Start-Process -Wait -FilePath '{q}' -ArgumentList '/Q','/T:\"{tmp}\"'")
                lines.append(f"Start-Process -Wait -FilePath '{tmp}\\DXSETUP.exe' -ArgumentList '/silent'")
            elif kind == "msi" or path.lower().endswith(".msi"):
                lines.append(f"Start-Process -Wait -FilePath msiexec.exe -ArgumentList '/i','\"{q}\"','/qn','/norestart'")
            else:
                args = ",".join(f"'{a}'" for a in spec.get("args", "").split())
                lines.append(f"Start-Process -Wait -FilePath '{q}'" + (f" -ArgumentList {args}" if args else ""))
        for p in pages:
            try:
                os.startfile(p)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if len(lines) > 1:
            run_elevated(self.cfg, lines, self.log)
        self.log("Finished. Re-checking this PC.")
        self.status["done"] = time.time()
        self.status["running"] = False


def run_elevated(cfg, ps_lines: list, log) -> int:
    """Runs PowerShell lines as admin (one UAC prompt) and copies the script's output into log()."""
    if not IS_WIN:
        log("Not Windows: skipped " + "; ".join(ps_lines))
        return 1
    import ctypes
    from ctypes import wintypes
    d = cfg.sub("cache")
    stamp = str(int(time.time() * 1000))
    script = os.path.join(d, f"elevated_{stamp}.ps1")
    out = os.path.join(d, f"elevated_{stamp}.log")
    body = "\r\n".join(ps_lines) + "\r\nWrite-Output 'ELEVATED_DONE'\r\n"
    with open(script, "w", encoding="utf-8-sig") as f:
        # Windows PowerShell's *> writes UTF-16, so pipe through Out-File -Encoding utf8 instead.
        # try/catch: a terminating error would otherwise stop the pipeline before its message reaches the log
        f.write(f"& {{ try {{\r\n{body}}} catch {{ Write-Output \"Error: $_\" }} }} *>&1 | ForEach-Object {{ \"$_\" }} | "
                f"Out-File -FilePath '{out}' -Encoding utf8\r\n")

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script]
    k32 = ctypes.windll.kernel32
    seen = 0

    def copy_output() -> None:
        nonlocal seen
        if os.path.exists(out):
            try:
                with open(out, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
                for ln in lines[seen:]:
                    if ln.strip():
                        log(ln.strip().lstrip("\ufeff"))
                seen = len(lines)
            except OSError:
                pass

    if is_admin():
        # Already elevated (run_admin.bat): Windows shows no prompt for "runas" then, so just run it and say so.
        log("Running as administrator already: no prompt needed.")
        proc = subprocess.Popen(["powershell.exe"] + args, creationflags=0x08000000)   # CREATE_NO_WINDOW
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                copy_output()
        copy_output()
        return proc.returncode

    sei = SEI()
    sei.cbSize = ctypes.sizeof(SEI)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    # Owned by the window in front (normally the browser), so the prompt opens on top instead of only flashing
    # a shield on the taskbar.
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
    sei.hwnd = ctypes.windll.user32.GetForegroundWindow()
    sei.lpVerb = "runas"
    sei.lpFile = "powershell.exe"
    sei.lpParameters = " ".join(f'"{a}"' if " " in a else a for a in args)
    sei.nShow = 0
    log("Waiting for the admin prompt. If you don't see it, click the flashing shield icon on the taskbar.")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)   # GetLastError via ctypes.windll is unreliable
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        log("You said No to the admin prompt: nothing was changed." if err == 1223 else   # ERROR_CANCELLED
            f"The admin prompt couldn't be shown (Windows error {err}): nothing was changed.")
        return 1
    while True:
        rc = k32.WaitForSingleObject(sei.hProcess, 500)
        copy_output()
        if rc == 0:
            break
    copy_output()
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    k32.CloseHandle(sei.hProcess)
    return code.value


def is_admin() -> bool:
    if not IS_WIN:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False
