"""Media shared between players: Movies, TV, Music, Books and Misc, next to the games.

Each category has one folder on this PC (Settings, or the button on the category's Library tab; settings key
"media_folders"). Every entry at the top of that folder is one item: a sub-folder (a series, an album, a box set)
or a single file (a movie, a PDF). Downloads from other players land in the same folder.

Items travel through the game copier unchanged (transfer.py: every player who has it at once, swarm, zstd where it
helps, SHA-256 checked). Their ids start with "media-<category>-", which no game id can (game ids are letters and
digits only), so the /peer/manifest, /peer/file and /peer/bundle endpoints serve both. Peers list media on
/peer/media; launchers older than 1.13.0 don't have it and simply show no media."""
import json
import os
import threading
import time

from .known import norm

CATEGORIES = {"movies": "Movies", "tv": "TV", "music": "Music", "books": "Books", "misc": "Misc"}
PREFIX = "media-"
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "lan.json", "lanparty.json"}
SKIP_SUFFIX = (".lanparty-part", ".lanparty-progress", ".lanparty-progress.tmp", ".tmp", ".crdownload", ".part")
MAX_WALK_FILES = 200000


def is_media_id(gid: str) -> bool:
    return isinstance(gid, str) and gid.startswith(PREFIX)


def category_of(gid: str) -> str:
    """The category of a media id, or '' if it isn't one we know."""
    if not is_media_id(gid):
        return ""
    cat = gid[len(PREFIX):].split("-", 1)[0]
    return cat if cat in CATEGORIES else ""


def make_id(cat: str, name: str) -> str:
    return f"{PREFIX}{cat}-{norm(name)[:80]}"


def _skip(name: str) -> bool:
    low = name.lower()
    return low.startswith((".", "$", "~$")) or low in SKIP_NAMES or low.endswith(SKIP_SUFFIX)


class Media:
    def __init__(self, cfg, hashes=None):
        self.cfg = cfg
        self.hashes = hashes                 # library.HashCache, shared with the games
        self.items = {}                      # id -> record (with local "path")
        self.scanning = False
        self.last_scan = 0.0
        self.on_change = None
        self._lock = threading.Lock()
        self.cache_path = os.path.join(cfg.data_dir, "media_cache.json")
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                self.items = {m["id"]: m for m in json.load(f)}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    # ---- folders ----
    def folders(self) -> dict:
        raw = self.cfg.get("media_folders") or {}
        return {c: str(raw.get(c) or "") for c in CATEGORIES} if isinstance(raw, dict) else {c: "" for c in CATEGORIES}

    def folder(self, cat: str) -> str:
        return self.folders().get(cat, "")

    def set_folder(self, cat: str, path: str) -> None:
        f = self.folders()
        f[cat] = path
        self.cfg.set("media_folders", f)

    # ---- listing ----
    def public_list(self) -> list:
        with self._lock:
            return [{k: v for k, v in m.items() if k != "path"} for m in self.items.values()]

    def get(self, mid: str):
        with self._lock:
            return self.items.get(mid)

    def rescan_async(self) -> None:
        if not self.scanning:
            threading.Thread(target=self.rescan, daemon=True).start()

    def rescan(self) -> None:
        self.scanning = True
        try:
            found = {}
            for cat, folder in self.folders().items():
                if not folder or not os.path.isdir(folder):
                    continue
                try:
                    entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
                except OSError:
                    continue
                for e in entries:
                    if _skip(e.name):
                        continue
                    rec = self.scan_item(cat, e.path)
                    if rec and rec["id"] not in found:
                        found[rec["id"]] = rec
            with self._lock:
                self.items = found
            try:
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(list(found.values()), f, indent=1)
            except OSError:
                pass
            self.last_scan = time.time()
            if self.hashes is not None:      # SHA-256 in the background, after the games (pauses while playing)
                self.hashes.hash_folders_async([m["path"] for m in found.values()])
        finally:
            self.scanning = False
        if self.on_change:
            self.on_change()

    @staticmethod
    def scan_item(cat: str, path: str):
        name = os.path.basename(path.rstrip("\\/"))
        mid = make_id(cat, name)
        if mid == make_id(cat, ""):
            return None
        single = os.path.isfile(path)
        size = nfiles = 0
        exts = {}
        if single:
            try:
                size, nfiles = os.path.getsize(path), 1
            except OSError:
                return None
            ext = os.path.splitext(name)[1].lower()
            if ext:
                exts[ext] = 1
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not _skip(d)]
                for fn in files:
                    if _skip(fn):
                        continue
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        continue
                    nfiles += 1
                    ext = os.path.splitext(fn)[1].lower()
                    if ext:
                        exts[ext] = exts.get(ext, 0) + 1
                if nfiles > MAX_WALK_FILES:
                    break
        else:
            return None
        title = os.path.splitext(name)[0] if single else name
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            mtime = 0
        top = [e for e, _ in sorted(exts.items(), key=lambda kv: -kv[1])[:3]]
        return {"id": mid, "cat": cat, "title": title.strip() or name, "name": name, "single": single,
                "path": path, "size": size, "files": nfiles, "types": top, "added": mtime}

    # ---- serving to peers (same shape as Library.manifest / resolve_file) ----
    def manifest(self, mid: str):
        m = self.get(mid)
        if not m:
            return None
        out = []
        if m["single"]:
            try:
                st = os.stat(m["path"])
            except OSError:
                return None
            e = {"p": m["name"], "size": st.st_size}
            sha = self.hashes.get(m["path"], st) if self.hashes is not None else ""
            if sha:
                e["sha256"] = sha
            out.append(e)
        else:
            base = m["path"]
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not _skip(d)]
                for fn in files:
                    if _skip(fn):
                        continue
                    full = os.path.join(root, fn)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    e = {"p": os.path.relpath(full, base).replace("\\", "/"), "size": st.st_size}
                    sha = self.hashes.get(full, st) if self.hashes is not None else ""
                    if sha:
                        e["sha256"] = sha
                    out.append(e)
        # "single": the one file goes straight into the category folder; otherwise into <folder>\<name>
        return {"id": mid, "title": m["title"], "folder": "" if m["single"] else m["name"], "single": m["single"],
                "cat": m["cat"], "files": out}

    def resolve_file(self, mid: str, rel: str):
        """Full path of a file inside a media item, or None if it isn't one of its files."""
        m = self.get(mid)
        if not m or not rel:
            return None
        if m["single"]:
            return m["path"] if rel == m["name"] and os.path.isfile(m["path"]) else None
        base = os.path.realpath(m["path"])
        full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if os.path.commonpath([base, full]) != base or not os.path.isfile(full):
            return None
        return full


def merge(mine: list, me: str, others: dict, alive: set) -> list:
    """Union of every player's media. owners = peer ids that have the item; have = this PC has it.
    others: peer id -> list of public records (a peer's /peer/media)."""
    out = {}
    for m in mine:
        out[m["id"]] = {**m, "owners": [me], "have": True}
    for pid, items in others.items():
        if pid not in alive or not isinstance(items, list):
            continue
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if not category_of(mid):
                continue
            if mid in out:
                if pid not in out[mid]["owners"]:
                    out[mid]["owners"].append(pid)
            else:
                clean = {"id": mid, "cat": category_of(mid), "title": str(m.get("title", ""))[:200],
                         "name": str(m.get("name", ""))[:260], "single": bool(m.get("single")),
                         "size": m.get("size") if isinstance(m.get("size"), int) else 0,
                         "files": m.get("files") if isinstance(m.get("files"), int) else 0,
                         "types": [str(t)[:10] for t in (m.get("types") or [])][:3] if isinstance(m.get("types"), list) else [],
                         "added": m.get("added") if isinstance(m.get("added"), int) else 0}
                out[mid] = {**clean, "owners": [pid], "have": False}
    return sorted(out.values(), key=lambda m: (m.get("cat", ""), m.get("title", "").lower()))
