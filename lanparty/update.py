"""Launcher versions, release zips, and updating the launcher from another PC that has a newer version.

A release is releases\\LanPartyLauncher_v<version>.zip: the code and text files (no bin\\, backup\\, settings) plus
manifest.json with the version and each file's SHA-256. Another launcher fetches it from /peer/app/release, but only
when its user clicks Update (/api/app/update, loopback). Before anything is written, every path and hash is checked;
files that will be replaced are backed up into backup\\<YYYY-MM-DD_HHMM>_update\\."""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from . import VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_FILES = ("run.bat", "run_admin.bat", "README.md")
TREE_DIRS = (("lanparty", ".py"), ("lanparty/web", ""), ("lanparty/vendor", ""), ("data", ""), ("docs", ""), ("tests", ".py"), ("tools", ".py"))
MANIFEST = "manifest.json"
MAX_RELEASE_BYTES = 50 << 20
_SKIP_NAMES = ("key.txt", "settings.json")
_SKIP_EXT = (".zip", ".rar", ".pyc", ".pyo")


def parse_version(v) -> tuple:
    """'1.10.2' -> (1, 10, 2). Anything else -> (0,), older than every real version."""
    m = re.fullmatch(r"\s*(\d{1,6}(?:\.\d{1,6}){0,3})\s*", str(v or ""))
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def is_newer(theirs, ours=VERSION) -> bool:
    t, o = parse_version(theirs), parse_version(ours)
    n = max(len(t), len(o))
    return t + (0,) * (n - len(t)) > o + (0,) * (n - len(o))


def _skip(name: str) -> bool:
    n = name.lower()
    return n in _SKIP_NAMES or n.endswith(_SKIP_EXT)


def release_files(root: str = ROOT) -> list:
    """Relative paths (with /) of the files that make up a release."""
    out = [f for f in ROOT_FILES if os.path.isfile(os.path.join(root, f))]
    for d, ext in TREE_DIRS:
        base = os.path.join(root, *d.split("/"))
        if not os.path.isdir(base):
            continue
        if ext:            # this folder's own files only (lanparty\*.py, tests\*.py, tools\*.py)
            names = sorted(f for f in os.listdir(base) if f.endswith(ext) and os.path.isfile(os.path.join(base, f)))
            out += [f"{d}/{f}" for f in names if not _skip(f)]
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = sorted(x for x in dns if x != "__pycache__")
            for fn in sorted(fns):
                if not _skip(fn):
                    out.append(os.path.relpath(os.path.join(dp, fn), root).replace("\\", "/"))
    return list(dict.fromkeys(out))


def release_path(root: str = ROOT, version: str = VERSION) -> str:
    return os.path.join(root, "releases", f"LanPartyLauncher_v{version}.zip")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_release(root: str = ROOT, version: str = VERSION, out: str = "") -> str:
    """Builds the release zip and returns its path."""
    out = out or release_path(root, version)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    files = {}
    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in release_files(root):
            with open(os.path.join(root, *rel.split("/")), "rb") as f:
                data = f.read()
            files[rel] = {"sha256": _sha(data), "size": len(data)}
            z.writestr(rel, data)
        z.writestr(MANIFEST, json.dumps({"app": "lanparty", "version": version, "built": time.time(),
                                         "files": files}, indent=1))
    os.replace(tmp, out)
    return out


_build_lock = threading.Lock()


def current_release(root: str = ROOT, version: str = VERSION) -> str:
    """This launcher's release zip, rebuilt when it's missing or a file changed after it was made."""
    with _build_lock:
        p = release_path(root, version)
        try:
            at = os.path.getmtime(p)
            if all(os.path.getmtime(os.path.join(root, *r.split("/"))) <= at for r in release_files(root)):
                return p
        except OSError:
            pass
        return make_release(root, version)


def safe_target(rel: str, root: str = ROOT) -> str:
    """Full path a release file may be written to. Raises ValueError for anything outside the allowed set."""
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or rel.startswith("/") or "\0" in rel:
        raise ValueError(f"bad path: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"bad path: {rel!r}")
    low = [p.lower() for p in parts]
    if "__pycache__" in low or _skip(parts[-1]):
        raise ValueError(f"not allowed in an update: {rel}")
    if low[:2] == ["lanparty", "bin"] or low[0] in ("backup", "releases"):
        raise ValueError(f"not allowed in an update: {rel}")
    if len(parts) == 1:
        if rel not in ROOT_FILES:
            raise ValueError(f"not allowed in an update: {rel}")
    elif low[0] not in ("lanparty", "data", "docs", "tests", "tools"):
        raise ValueError(f"not allowed in an update: {rel}")
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, *parts))
    if os.path.commonpath([base, full]) != base:
        raise ValueError(f"outside the launcher folder: {rel}")
    return full


def check_release(zip_path: str, root: str = ROOT) -> dict:
    """Reads and verifies a release zip. Returns {"version", "files": {rel: bytes}}; raises ValueError."""
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ValueError(f"not a release zip ({e})")
    with z:
        try:
            man = json.loads(z.read(MANIFEST).decode("utf-8"))
        except (KeyError, ValueError) as e:
            raise ValueError(f"release has no valid manifest ({e})")
        if not isinstance(man, dict) or man.get("app") != "lanparty" or not isinstance(man.get("files"), dict):
            raise ValueError("release manifest is not a launcher manifest")
        version = str(man.get("version", ""))
        if parse_version(version) == (0,):
            raise ValueError(f"release has a bad version: {version!r}")
        names = [n for n in z.namelist() if n != MANIFEST]
        if set(names) != set(man["files"]):
            raise ValueError("release files don't match its manifest")
        out, total = {}, 0
        for rel in names:
            if "/" not in rel and rel not in ROOT_FILES and rel.lower().endswith(".md"):
                continue        # an extra notes file at the top of a peer's copy: not needed, not written
            safe_target(rel, root)
            info = z.getinfo(rel)
            total += info.file_size
            if total > MAX_RELEASE_BYTES * 4:
                raise ValueError("release is too big")
            data = z.read(rel)
            want = man["files"][rel]
            if not isinstance(want, dict) or _sha(data) != want.get("sha256"):
                raise ValueError(f"hash mismatch: {rel}")
            out[rel] = data
        if "lanparty/__init__.py" not in out:
            raise ValueError("release has no lanparty/__init__.py")
    return {"version": version, "files": out}


def apply_release(zip_path: str, root: str = ROOT, require_newer: bool = True) -> dict:
    """Verifies, backs up the files it replaces, and writes the release over the launcher folder.
    Returns {"version", "written", "backup"}; raises ValueError (nothing is written then)."""
    rel = check_release(zip_path, root)
    if require_newer and not is_newer(rel["version"], installed_version(root)):
        raise ValueError(f"v{rel['version']} is not newer than v{installed_version(root)}")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    bdir = os.path.join(root, "backup", stamp + "_update")
    n = 1
    while os.path.exists(bdir):
        n += 1
        bdir = os.path.join(root, "backup", f"{stamp}_update{n}")
    written = []
    for name, data in sorted(rel["files"].items()):
        full = safe_target(name, root)
        if os.path.isfile(full):
            with open(full, "rb") as f:
                old = f.read()
            if old == data:
                continue
            bk = os.path.join(bdir, *name.split("/"))
            os.makedirs(os.path.dirname(bk), exist_ok=True)
            with open(bk, "wb") as f:
                f.write(old)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".lanparty-new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
        written.append(name)
    return {"version": rel["version"], "written": written, "backup": bdir if os.path.isdir(bdir) else ""}


def installed_version(root: str = ROOT) -> str:
    """VERSION as written in <root>\\lanparty\\__init__.py (after an update, newer than the running one)."""
    try:
        with open(os.path.join(root, "lanparty", "__init__.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else VERSION
    except OSError:
        return VERSION


def restart_argv(argv: list = None) -> list:
    """Command line for the restarted launcher: same Python and options, waits for the port, no new browser tab."""
    args = list(sys.argv[1:] if argv is None else argv)
    clean, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--wait-port":
            skip = True
            continue
        if a.startswith("--wait-port="):
            continue
        clean.append(a)
    if "--no-browser" not in clean:
        clean.append("--no-browser")
    return [sys.executable, "-m", "lanparty"] + clean + ["--wait-port", "30"]


class Updater:
    """Downloads a newer release from a peer, applies it and restarts. Only started from /api/app/update."""

    def __init__(self, app, root: str = ROOT):
        self.app = app
        self.root = root
        self.status = {"state": "idle", "msg": "", "version": "", "from": ""}
        self._lock = threading.Lock()

    def available(self) -> dict:
        """The newest version a peer online has, if it's newer than ours: {peer_id, name, version} or {}."""
        best = {}
        for p in self.app.discovery.list():
            v = str(p.get("version", ""))
            if is_newer(v) and (not best or is_newer(v, best["version"])):
                best = {"peer_id": p["id"], "name": p["name"], "version": v}
        return best

    def start(self, peer_id: str) -> str:
        with self._lock:
            if self.status["state"] in ("downloading", "applying", "restarting"):
                return "An update is already running."
            p = self.app.discovery.get(peer_id)
            if not p:
                return "That player isn't online."
            if not is_newer(p.get("version", "")):
                return f"{p['name']} has v{p.get('version', '?')}, not newer than your v{VERSION}."
            self.status = {"state": "downloading", "msg": f"Downloading v{p['version']} from {p['name']}…",
                           "version": str(p["version"]), "from": p["name"]}
        threading.Thread(target=self._run, args=(p,), daemon=True).start()
        return "ok"

    def _set(self, state: str, msg: str) -> None:
        self.status = dict(self.status, state=state, msg=msg)
        self.app.note(msg)

    def _download(self, p: dict, dest: str) -> None:
        from . import peers
        path = "/peer/app/release"
        req = urllib.request.Request(peers.peer_url(p, path, peers.best_ip(p)),
                                     headers=peers._sign_headers("GET", path, b""))
        got = 0
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_RELEASE_BYTES:
                    raise ValueError("release is too big")
                f.write(chunk)

    def _run(self, p: dict) -> None:
        dest = os.path.join(self.app.cfg.data_dir, "update_incoming.zip")
        try:
            self._download(p, dest)
            self._set("applying", "Checking and installing the update…")
            r = apply_release(dest, self.root)
            if r["version"] != str(p["version"]):
                self.app.note(f"{p['name']} sent v{r['version']} (it advertised v{p['version']}).")
            self.status["version"] = r["version"]
            where = f" Old files backed up in {r['backup']}." if r["backup"] else ""
            party = " The internet party stays connected." if self.app.vpn.is_active() else ""
            self._set("restarting", f"Updated to v{r['version']} ({len(r['written'])} files).{where} "
                                    f"Restarting…{party}")
            restart(self.app, self.root)
        except Exception as e:  # noqa: BLE001 - every failure is shown to the user; nothing has been half-written
            self._set("error", f"Update failed: {e}")
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass


def restart(app, root: str = ROOT, delay: float = 1.0) -> None:
    """Starts a new launcher (same Python, options and elevation: a child of this process) and exits this one.
    An internet party is handed over to the new launcher (handoff.py): n2n keeps running, the network settings stay,
    running games keep their connection. Only if that can't be done is the party left and the settings restored."""
    from .vpn import restore_game_network
    kept = False
    if app.vpn.is_active():
        try:
            kept = bool(app.hand_over_party())
        except Exception:  # noqa: BLE001
            kept = False
        if not kept:
            app.note("Couldn't keep the internet party through the restart; leaving it.")
            try:
                app.vpn.stop()
            except Exception:  # noqa: BLE001
                pass
    if not kept:
        try:
            restore_game_network(app.cfg.data_dir, app.vpn.log)
        except Exception:  # noqa: BLE001
            pass
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
    subprocess.Popen(restart_argv(), cwd=root, creationflags=flags)
    threading.Timer(delay, lambda: os._exit(0)).start()
