"""Scans the player's game folders. Every sub-folder of a games folder is one game."""
import json
import os
import re
import threading
import time

from . import namewrite, pe, prereqs
from .hashcache import HashCache
from .known import GameInfo, KnownGames, norm
from .media import Media, is_media_id

SKIP_EXE = re.compile(r"(setup|install|unins|redist|dxsetup|dotnet|ndp\d|crash|report|updat|patch|config|"
                      r"helper|ue4prereq|directx|physx|oalinst|vc_?redist|cleanup|register|activation|dosbox)", re.I)
MAX_WALK_FILES = 200000


def _real_case(base: str, rel_lower: str) -> str:
    """The on-disk spelling of a lower-cased relative path (the scan keeps names lower-case)."""
    out, cur = [], base
    for part in rel_lower.split(os.sep):
        try:
            match = next((n for n in os.listdir(cur) if n.lower() == part), part)
        except OSError:
            match = part
        out.append(match)
        cur = os.path.join(cur, match)
    return os.sep.join(out)


def _title_from_folder(name: str) -> str:
    return re.sub(r"[_\.]+", " ", name).strip()


_PORTS = re.compile(r"^\s*\d{1,5}(\s*-\s*\d{1,5})?(\s*,\s*\d{1,5}(\s*-\s*\d{1,5})?)*\s*$")


def _str(v, n: int = 260) -> str:
    return str(v)[:n] if isinstance(v, (str, int, float)) else ""


def _cmd(spec) -> dict:
    if not isinstance(spec, dict):
        return {}
    args = spec.get("args", [])
    if isinstance(args, str):
        args = [args]
    out = {"command": _str(spec.get("command", "")), "args": [_str(a, 1000) for a in args if _str(a, 1000)][:64]}
    if spec.get("then_join"):
        out["then_join"] = True
    return out


def load_lan_json(path: str) -> dict:
    """Reads <game>\\lan.json into a cleaned dict. Unknown keys are dropped; bad values are ignored."""
    p = os.path.join(path, "lan.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"error": "lan.json is not valid JSON"}
    if not isinstance(raw, dict):
        return {"error": "lan.json must be an object"}
    lan = {}
    if raw.get("game"):
        lan["game"] = _str(raw["game"], 80)
    if isinstance(raw.get("genre"), str) and raw["genre"].strip():      # library filters
        lan["genre"] = _str(raw["genre"], 30)
    if isinstance(raw.get("year"), int) and 1970 <= raw["year"] <= 2100:
        lan["year"] = raw["year"]
    if raw.get("exe"):
        lan["exe"] = _str(raw["exe"]).replace("/", "\\")
    if isinstance(raw.get("args"), str) and raw["args"].strip():   # extra command line for Launch (gear dialog)
        lan["args"] = _str(raw["args"].replace("\r", " ").replace("\n", " "), 500)
    setup = raw.get("setup")
    if isinstance(setup, dict) and setup.get("script"):
        lan["setup"] = _str(setup["script"]).replace("/", "\\")
    elif isinstance(setup, str) and setup:
        lan["setup"] = _str(setup).replace("/", "\\")
    for role in ("host", "join"):
        c = _cmd(raw.get(role))
        if c:
            lan[role] = c
    ports = raw.get("ports")
    if isinstance(ports, dict):
        clean = {k: _str(ports[k], 200) for k in ("udp", "tcp") if _PORTS.match(_str(ports.get(k, ""), 200) or "x")}
        if clean:
            lan["ports"] = clean
    fw = raw.get("firewall")
    if isinstance(fw, list):
        lan["firewall"] = [_str(x, 500) for x in fw if _str(x, 500)][:32]
    fb = raw.get("force_bind")        # false: never through ForceBindIP; "delayed": inject after the window opens
    if fb is False or fb == "delayed":
        lan["force_bind"] = fb
    names = namewrite.clean_entries(raw.get("name"))
    if names:
        lan["name"] = names
    pl = raw.get("players")
    if isinstance(pl, dict):
        try:
            lan["players"] = {"min": max(1, int(pl.get("min", 1))), "max": max(1, int(pl.get("max", 64)))}
        except (TypeError, ValueError):
            pass
    return lan


class Library:
    def __init__(self, cfg):
        self.cfg = cfg
        self.known = KnownGames()
        self.info = GameInfo()
        self.games = {}          # id -> record (with local "path")
        self.scanning = False
        self.last_scan = 0.0
        self.cache_path = os.path.join(cfg.data_dir, "library_cache.json")
        self._lock = threading.Lock()
        self.on_change = None
        self.hashes = HashCache(cfg.data_dir)     # SHA-256 per file, for the manifests (verify every copy)
        self.media = Media(cfg, self.hashes)       # Movies / TV / Music / Books / Misc (media.py), same copier
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                self.games = {g["id"]: g for g in json.load(f)}
        except (OSError, ValueError, KeyError):
            pass

    # ---- public ----
    def public_list(self) -> list:
        """What peers see: no local paths."""
        with self._lock:
            # no local path; the exe list is only for this PC's gear dialog (server.state adds it back)
            return [{k: v for k, v in g.items() if k not in ("path", "exes")} for g in self.games.values()]

    def get(self, gid: str):
        with self._lock:
            return self.games.get(gid)

    def has(self, gid: str) -> bool:
        """Whether this PC has the game or media item (what /peer/file and /peer/bundle can serve whole)."""
        return (self.media.get(gid) if is_media_id(gid) else self.get(gid)) is not None

    def rescan_async(self) -> None:
        if self.scanning:
            return
        threading.Thread(target=self.rescan, daemon=True).start()

    def rescan(self) -> None:
        self.scanning = True
        try:
            found = {}
            for folder in self.cfg["folders"]:
                if not os.path.isdir(folder):
                    continue
                try:
                    entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
                except OSError:
                    continue
                for e in entries:
                    if e.is_dir() and not e.name.startswith((".", "$")):
                        rec = self.scan_game(e.path)
                        if rec and rec["id"] not in found:
                            found[rec["id"]] = rec
            with self._lock:
                self.games = found
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(list(found.values()), f, indent=1)
            self.last_scan = time.time()
            self.hashes.hash_folders_async([g["path"] for g in found.values()])
        finally:
            self.scanning = False
        if self.on_change:
            self.on_change()

    # ---- one game ----
    def scan_game(self, path: str):
        folder = os.path.basename(path.rstrip("\\/"))
        manifest = {}
        mpath = os.path.join(path, "lanparty.json")
        if os.path.exists(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (OSError, ValueError):
                manifest = {}
        lan = load_lan_json(path)
        if lan.get("game"):
            manifest.setdefault("title", lan["game"])
        if lan.get("exe"):
            manifest["exe"] = lan["exe"]
        known = self.known.lookup(manifest.get("title", folder)) or self.known.lookup(folder) or {}
        title = manifest.get("title") or known.get("title") or _title_from_folder(folder)
        gid = manifest.get("id") or known.get("id") or norm(folder)
        if not gid:
            return None

        size = 0
        nfiles = 0
        exes = []          # (rel, depth, size)
        redists = []       # (prereq id, rel)
        has_xnb = False
        files_by_dir = {}
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel_root = os.path.relpath(root, path)
            depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
            files_by_dir[rel_root] = {f.lower() for f in files}
            for fn in files:
                nfiles += 1
                full = os.path.join(root, fn)
                try:
                    fsz = os.path.getsize(full)
                except OSError:
                    continue
                size += fsz
                rel = os.path.normpath(os.path.join(rel_root, fn)) if rel_root != "." else fn
                low = fn.lower()
                if low.endswith(".xnb"):
                    has_xnb = True
                if low.endswith((".exe", ".msi")):
                    pr = prereqs.classify_redist(rel)
                    if pr:
                        redists.append((pr, rel))
                        continue
                if low.endswith((".exe", ".com", ".bat")) and depth <= 3 and not SKIP_EXE.search(low):
                    exes.append((rel, depth, fsz))
            if nfiles > MAX_WALK_FILES:
                break

        # every program in the folder, for the gear dialog's Executable list (setup/redist ones included, with a hint)
        all_exes = []
        for rel_root, names in files_by_dir.items():
            if rel_root != "." and rel_root.count(os.sep) >= 4:
                continue
            for low in names:
                if low.endswith(".exe"):
                    rel = low if rel_root == "." else os.path.join(rel_root, low)
                    all_exes.append(rel)
        exe_list = []
        for rel in sorted(all_exes, key=lambda r: (r.count(os.sep), r))[:60]:
            real = _real_case(path, rel)
            try:
                kb = os.path.getsize(os.path.join(path, real)) // 1024
            except OSError:
                continue
            low = os.path.basename(real).lower()
            hint = ("launcher" if "launch" in low else "setup / uninstaller" if re.search(r"setup|install|unins", low)
                    else "redistributable" if prereqs.classify_redist(real) else "tool" if SKIP_EXE.search(low) else "")
            exe_list.append({"rel": real, "kb": kb, "hint": hint})
        exe_rels = {e["rel"].lower(): e["rel"] for e in exe_list}

        # automatic pick (shown as "Automatic" in the gear dialog)
        auto = ""
        if known.get("exe"):
            for rel, _, _ in exes:
                if os.path.basename(rel).lower() == known["exe"].lower():
                    auto = rel
                    break
        if not auto and exes:
            key = norm(folder)

            def score(item):
                rel, depth, fsz = item
                stem = norm(os.path.splitext(os.path.basename(rel))[0])
                named = 1 if stem and (stem in key or key.startswith(stem[:4])) else 0
                is_exe = 1 if rel.lower().endswith(".exe") else 0
                return (named, -depth, is_exe, fsz)
            auto = max(exes, key=score)[0]
        # this PC's choice (gear dialog, settings game_overrides) > lan.json / lanparty.json > automatic.
        # An override only counts if it's one of the programs found inside this game folder.
        ov = ((self.cfg.get("game_overrides", {}) or {}).get(gid) or {}) if hasattr(self.cfg, "get") else {}
        ov_exe = exe_rels.get(str(ov.get("exe", "")).replace("/", "\\").lower(), "")
        if ov_exe:
            exe, exe_source = ov_exe, "this PC"
        elif manifest.get("exe"):
            exe = manifest["exe"]
            exe_source = "lan.json" if lan.get("exe") else "lanparty.json"
        else:
            exe, exe_source = auto, "automatic"
        args = manifest.get("args", known.get("args", ""))
        args_source = ""
        if lan.get("args"):
            args, args_source = lan["args"], "lan.json"
        if isinstance(ov.get("args"), str) and ov.get("args").strip():
            args, args_source = ov["args"][:500], "this PC"

        info = {"kind": "none", "arch": "", "imports": set(), "clr": None}
        needs = set(manifest.get("prereqs", []))
        ipx_hint = False
        if exe:
            full_exe = os.path.join(path, exe)
            info = pe.read_exe(full_exe)
            exe_dir = os.path.dirname(exe) or "."
            local = files_by_dir.get(os.path.normpath(exe_dir) if exe_dir != "." else ".", set())
            dlls = set(info["imports"])
            # Imports of the game's own DLLs count too (many engines put d3dx/vc in a renderer DLL).
            for fn in sorted(local):
                if fn.endswith(".dll") and len(dlls) < 400:
                    dlls |= pe.read_exe(os.path.join(path, exe_dir, fn))["imports"]
            needs |= prereqs.needs_from_imports(dlls, info["arch"], info["clr"], local)
            if info["kind"] in ("pe", "dos") and "ipx" not in manifest.get("network", ""):
                ipx_hint = pe.contains_ipx_strings(full_exe)
        if has_xnb:
            needs.add("xna40")
        network = manifest.get("network") or known.get("network") or ("ipx" if ipx_hint else "")
        dos = bool(manifest.get("dos", known.get("dos", info["kind"] == "dos")))
        # library filters: lan.json > data\game_info.json (never guessed; missing = shown as Unknown)
        gi = self.info.lookup(title, folder, gid)
        meta = {"genre": lan.get("genre") or gi.get("genre", ""), "year": lan.get("year") or gi.get("year", 0),
                "players": lan.get("players") or gi.get("players") or {}}
        return {
            **meta,
            "id": gid,
            "title": title,
            "folder": folder,
            "path": path,
            "exe": exe,
            "auto_exe": auto,
            "default_exe": manifest.get("exe") or auto,           # what "Default" in the gear dialog means
            "default_source": ("lan.json" if lan.get("exe") else "lanparty.json") if manifest.get("exe") else "automatic",
            "exe_source": exe_source,
            "exes": exe_list,
            "args": args,
            "args_source": args_source,
            "has_reg": any(n.endswith(".reg") for n in files_by_dir.get(".", set())),
            "host_args": manifest.get("host_args", known.get("host_args", "")),
            "join_args": manifest.get("join_args", known.get("join_args", "")),
            "kind": "dos" if dos else info["kind"],
            "arch": info["arch"],
            "size": size,
            "files": nfiles,
            "prereqs": sorted(needs),
            "redists": [{"id": p, "rel": r} for p, r in redists],
            "network": network,
            "ipx_detected": ipx_hint,
            "lan": lan,
        }

    # ---- serving files to peers ----
    def manifest(self, gid: str):
        if is_media_id(gid):
            return self.media.manifest(gid)
        g = self.get(gid)
        if not g:
            return None
        out = []
        base = g["path"]
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                full = os.path.join(root, fn)
                if fn.endswith(".lanparty-part"):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if fn.endswith(".lanparty-progress"):
                    continue
                e = {"p": os.path.relpath(full, base).replace("\\", "/"), "size": st.st_size}
                sha = self.hashes.get(full, st)
                if sha:
                    e["sha256"] = sha
                out.append(e)
        return {"id": gid, "title": g["title"], "folder": g["folder"], "files": out}

    def resolve_file(self, gid: str, rel: str):
        """Full path of a file inside a game, or None if it escapes the game folder."""
        if is_media_id(gid):
            return self.media.resolve_file(gid, rel)
        g = self.get(gid)
        if not g or not rel:
            return None
        base = os.path.realpath(g["path"])
        full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if os.path.commonpath([base, full]) != base or not os.path.isfile(full):
            return None
        return full
