"""Known-games database: maps folder names to a shared id and carries network/launch hints."""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(_HERE), "data", "known_games.json")


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))


class KnownGames:
    def __init__(self, path: str = DB_PATH):
        self.by_key = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                games = json.load(f).get("games", [])
        except (OSError, ValueError):
            games = []
        for g in games:
            for key in [g["id"], norm(g.get("title", ""))] + [norm(a) for a in g.get("aliases", [])]:
                if key:
                    self.by_key[key] = g

    def lookup(self, folder_name: str):
        return self.by_key.get(norm(folder_name))


INFO_PATH = os.path.join(os.path.dirname(_HERE), "data", "game_info.json")


class GameInfo:
    """Genre / release year / player counts for the library filters (data\\game_info.json). Separate from
    known_games.json on purpose: matching here never changes a game's id, which peers use to merge libraries."""

    def __init__(self, path: str = INFO_PATH):
        self.by_key = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                games = json.load(f).get("games", [])
        except (OSError, ValueError):
            games = []
        for g in games:
            for k in g.get("keys", []):
                self.by_key[norm(k)] = g

    def lookup(self, *names) -> dict:
        """{genre, year, players} for the first name that matches (title, folder, id), else {}."""
        for n in names:
            g = self.by_key.get(norm(n or ""))
            if g:
                return {k: g[k] for k in ("genre", "year", "players") if k in g}
        return {}
