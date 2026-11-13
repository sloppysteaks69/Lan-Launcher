"""SHA-256 of every shared game file, worked out in the background and kept between runs, so every copy is checked.

hash_cache.json in the data folder maps a file's full path to [size, mtime_ns, sha256]; an entry only counts while
the file still has that size and time. After each library scan a background thread hashes the files it doesn't
know yet (pausing while a game runs, see throttle.PLAYING). The manifest a peer downloads carries the hashes known
so far, and the downloader checks each file against them. Files a download just verified are added straight away."""
import hashlib
import json
import os
import threading
import time

from . import throttle

IO = 1 << 20
SAVE_EVERY = 30.0


class HashCache:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "hash_cache.json")
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self.data = {}
        self.pending = 0          # files still to hash in the current pass
        self.dirty = False
        self._queued = []         # folders asked for while a pass was running (games, then media): done next
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.data = {k: v for k, v in raw.items() if isinstance(v, list) and len(v) == 3}
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _key(full: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(full)))

    def get(self, full: str, st: os.stat_result = None) -> str:
        """The file's SHA-256 if known for its current size and time, else ''."""
        try:
            st = st or os.stat(full)
        except OSError:
            return ""
        with self._lock:
            e = self.data.get(self._key(full))
        return e[2] if e and e[0] == st.st_size and e[1] == st.st_mtime_ns else ""

    def put(self, full: str, sha: str) -> None:
        try:
            st = os.stat(full)
        except OSError:
            return
        with self._lock:
            self.data[self._key(full)] = [st.st_size, st.st_mtime_ns, sha]
            self.dirty = True

    def save(self) -> None:
        with self._lock:
            if not self.dirty:
                return
            snap = dict(self.data)
            self.dirty = False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def sha256(full: str, pause=lambda: False) -> str:
        h = hashlib.sha256()
        with open(full, "rb") as f:
            while True:
                while pause():
                    time.sleep(1.0)
                b = f.read(IO)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    def hash_folders_async(self, folders: list) -> None:
        threading.Thread(target=self.hash_folders, args=(list(folders),), daemon=True).start()

    def hash_folders(self, folders: list) -> None:
        """Hash every file under these folders (or these single files: media) that isn't known yet. One pass at a
        time; folders asked for during a pass are hashed right after it."""
        with self._lock:
            self._queued.extend(folders)
        while True:
            if not self._busy.acquire(blocking=False):
                return
            try:
                with self._lock:
                    todo_dirs = list(dict.fromkeys(self._queued))
                    self._queued.clear()
                if todo_dirs:
                    self._pass(todo_dirs)
            finally:
                self._busy.release()
            with self._lock:          # asked for while we were finishing: go round again
                if not self._queued:
                    return

    def _pass(self, folders: list) -> None:
        todo = []
        for base in folders:
            if os.path.isfile(base):
                try:
                    st = os.stat(base)
                except OSError:
                    continue
                if not self.get(base, st):
                    todo.append((base, st))
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fn in files:
                    if fn.endswith((".lanparty-part", ".lanparty-progress", ".tmp")):
                        continue
                    full = os.path.join(root, fn)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    if not self.get(full, st):
                        todo.append((full, st))
        self.pending = len(todo)
        last = time.time()
        for full, st in todo:
            try:
                sha = self.sha256(full, pause=lambda: throttle.PLAYING.active)
                st2 = os.stat(full)
                if st2.st_size == st.st_size and st2.st_mtime_ns == st.st_mtime_ns:   # unchanged while read
                    with self._lock:
                        self.data[self._key(full)] = [st.st_size, st.st_mtime_ns, sha]
                        self.dirty = True
            except OSError:
                pass
            self.pending -= 1
            if time.time() - last > SAVE_EVERY:
                self.save()
                last = time.time()
        self.pending = 0
        self.save()
