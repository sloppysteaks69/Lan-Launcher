"""Settings saved as JSON in the data folder (default %APPDATA%\\LanPartyLauncher)."""
import json
import os
import socket
import threading
import uuid


def default_data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LanPartyLauncher")


class Config:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "settings.json")
        self._lock = threading.Lock()
        self.data = {
            "peer_id": uuid.uuid4().hex[:12],
            "name": socket.gethostname(),
            "folders": [],
            "download_dir": "",
            "dosbox_path": "",
            "static_peers": [],
            "share_library": True,
            # Party Master: the one player who starts polls and sees the party's heart scores (advertised to peers)
            "party_master": False,
            # while a game runs on this PC, game copies to and from it are held to this many MB/s each way
            "limit_while_playing": True,
            "limit_mbps": 1.0,
            # Library > Movies / TV / Music / Books / Misc: one folder each (media.py), shared like the games
            "media_folders": {},
            "share_media": True,
        }
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError):
                pass
        self.save()

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        with self._lock:
            self.data[key] = value
        self.save()

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def sub(self, *parts: str) -> str:
        """A folder inside the data dir, created on demand."""
        p = os.path.join(self.data_dir, *parts)
        os.makedirs(p, exist_ok=True)
        return p
