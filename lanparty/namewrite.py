"""Writes the player's launcher name into a game's own settings before it starts, for games that can't take a
name on the command line. Driven by the "name" list in lan.json. The same entries may write this PC's address
({my_ip}: the VPN IP in an internet party, else the LAN IP) for games that pick the wrong adapter, e.g. AoE3's
OverrideAddress in Startup\\user.cfg.

Three kinds of entry:
  {"ini": "<file>", "section": "Player", "key": "Name", "value": "{player_name}"}
  {"text": "<file>", "pattern": "^seta name .*$", "line": "seta name \\"{player_name}\\""}
  {"registry": "HKCU\\\\Software\\\\Vendor\\\\Game", "value": "PlayerName", "data": "{player_name}", "type": "string"}

Limits, because a lan.json can arrive with a game copied from another player:
- files must be inside the game folder or the user's Documents / AppData, and be a config-type file;
- registry writes only under HKEY_CURRENT_USER\\Software;
- the first time a file is changed, the original is kept as <file>.lanparty-bak."""
import os
import re
import shutil
import sys

CONFIG_EXT = (".ini", ".cfg", ".con", ".conf", ".txt", ".xml", ".json", ".properties", ".lua", ".sav")
_KINDS = ("ini", "text", "registry")


def _user_dirs() -> dict:
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:   # CSIDL_PERSONAL
                docs = buf.value
        except (OSError, AttributeError):
            pass
    return {"{documents}": docs, "{appdata}": os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
            "{localappdata}": os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
            "{userprofile}": home}


def clean_entries(raw) -> list:
    """Keeps only well-formed entries from lan.json's "name" list."""
    out = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return out
    for e in raw[:16]:
        if not isinstance(e, dict):
            continue
        kind = next((k for k in _KINDS if isinstance(e.get(k), str) and e.get(k)), None)
        if not kind:
            continue
        item = {k: str(v)[:500] for k, v in e.items() if isinstance(v, (str, int)) and k in
                ("ini", "text", "registry", "section", "key", "value", "pattern", "line", "data", "type", "encoding")}
        item["kind"] = kind
        out.append(item)
    return out


_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _fill(s: str, name: str, game_dir: str, my_ip: str = "") -> str:
    return s.replace("{player_name}", name).replace("{game_dir}", game_dir).replace("{my_ip}", my_ip)


def resolve_file(game_dir: str, rel: str) -> str:
    """Full path for a name-entry file, or ValueError if it's not an allowed place or file type."""
    dirs = _user_dirs()
    root = game_dir      # a plain relative path must stay inside the game folder
    path = os.path.join(game_dir, rel)
    for k in ("{documents}", "{appdata}", "{localappdata}"):
        if rel.lower().startswith(k):
            root = dirs[k]
            path = root + rel[len(k):]
            break
    full = os.path.normpath(os.path.abspath(path))
    base = os.path.normcase(os.path.normpath(os.path.abspath(root)))
    if not os.path.normcase(full).startswith(base + os.sep):
        raise ValueError(f"{rel}: must stay inside the game folder, or start with {{documents}}, {{appdata}} "
                         f"or {{localappdata}} and stay inside it")
    if not full.lower().endswith(CONFIG_EXT):
        raise ValueError(f"{rel}: not a config file type ({', '.join(CONFIG_EXT)})")
    return full


def _read(path: str, encoding: str):
    """(text, encoding, newline). Detects UTF-16/UTF-8 BOMs; otherwise uses the given encoding or latin-1."""
    if not os.path.exists(path):
        return "", encoding or "utf-8", "\r\n"
    raw = open(path, "rb").read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        enc = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    else:
        enc = encoding or "latin-1"
    text = raw.decode(enc, "replace")
    nl = "\r\n" if "\r\n" in text or not text else "\n"
    return text, enc, nl


def _write(path: str, text: str, enc: str) -> None:
    if os.path.exists(path) and not os.path.exists(path + ".lanparty-bak"):
        shutil.copy2(path, path + ".lanparty-bak")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(text)


def set_ini(text: str, section: str, key: str, value: str, nl: str) -> str:
    """Sets key=value inside [section], adding the key or the section when missing. Other lines are kept as-is."""
    lines = text.split(nl) if text else []
    sec_re = re.compile(r"^\s*\[(.+?)\]\s*$")
    key_re = re.compile(r"^(\s*)" + re.escape(key) + r"(\s*)=", re.I)
    cur, sec_start, sec_end = None, None, None
    for i, ln in enumerate(lines):
        m = sec_re.match(ln)
        if m:
            if cur is not None and cur.lower() == section.lower() and sec_end is None:
                sec_end = i
            cur = m.group(1)
            if cur.lower() == section.lower() and sec_start is None:
                sec_start = i
            continue
        if cur is not None and cur.lower() == section.lower() and sec_end is None:
            km = key_re.match(ln)
            if km:
                lines[i] = f"{km.group(1)}{key}{km.group(2)}={value}"
                return nl.join(lines)
    new_line = f"{key}={value}"
    if sec_start is None:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ([""] if lines else []) + [f"[{section}]", new_line, ""]
        return nl.join(lines)
    end = sec_end if sec_end is not None else len(lines)
    while end > sec_start + 1 and lines[end - 1].strip() == "":
        end -= 1
    lines.insert(end, new_line)
    return nl.join(lines)


def set_text(text: str, pattern: str, line: str, nl: str) -> str:
    rx = re.compile(pattern, re.M)
    if rx.search(text):
        # In a CRLF file, '.*$' also takes the '\r' (re.M's $ is before '\n'): keep it, or the line ending breaks.
        return rx.sub(lambda m: line + ("\r" if m.group(0).endswith("\r") else ""), text, count=1)
    if text and not text.endswith(nl):
        text += nl
    return text + line + nl


def _set_registry(key: str, value: str, data: str, kind: str) -> None:
    if sys.platform != "win32":
        raise ValueError("registry entries only work on Windows")
    import winreg
    k = key.replace("/", "\\")
    for prefix in ("HKEY_CURRENT_USER\\", "HKCU\\"):
        if k.upper().startswith(prefix):
            k = k[len(prefix):]
            break
    else:
        raise ValueError(f"{key}: only HKCU\\Software\\... may be changed")
    if not k.lower().startswith("software\\") or ".." in k:
        raise ValueError(f"{key}: only HKCU\\Software\\... may be changed")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k, 0, winreg.KEY_SET_VALUE) as h:
        if kind == "dword":
            winreg.SetValueEx(h, value, 0, winreg.REG_DWORD, int(data))
        else:
            winreg.SetValueEx(h, value, 0, winreg.REG_SZ, data)


def apply(game: dict, player_name: str, my_ip: str = "") -> list:
    """Writes the name (and/or this PC's address, {my_ip}) for every entry in game['lan']['name'].
    Returns a list of problems (empty = all good)."""
    entries = (game.get("lan") or {}).get("name") or []
    problems = []
    gdir = game["path"]
    my_ip = my_ip if _IP_RE.match(my_ip or "") else ""
    for e in entries:
        try:
            template = e.get("line") if e["kind"] == "text" else e.get("data" if e["kind"] == "registry" else "value",
                                                                         "{player_name}")
            # The value must be the player's name or this PC's address (e.g. AoE3's OverrideAddress), nothing else.
            if "{player_name}" not in (template or "") and "{my_ip}" not in (template or ""):
                raise ValueError(f"{e[e['kind']]}: the value must contain {{player_name}} or {{my_ip}}")
            if "{my_ip}" in (template or "") and not my_ip:
                raise ValueError(f"{e[e['kind']]}: this PC's address isn't known")
            if e["kind"] == "registry":
                _set_registry(e["registry"], e.get("value", ""),
                              _fill(e.get("data", "{player_name}"), player_name, gdir, my_ip),
                              e.get("type", "string").lower())
                continue
            path = resolve_file(gdir, e[e["kind"]])
            text, enc, nl = _read(path, e.get("encoding", ""))
            if e["kind"] == "ini":
                if not e.get("section") or not e.get("key"):
                    raise ValueError("ini entries need section and key")
                new = set_ini(text, e["section"], e["key"],
                              _fill(e.get("value", "{player_name}"), player_name, gdir, my_ip), nl)
            else:
                if not e.get("pattern") or not e.get("line"):
                    raise ValueError("text entries need pattern and line")
                new = set_text(text, e["pattern"], _fill(e["line"], player_name, gdir, my_ip), nl)
            if new != text:
                _write(path, new, enc)
        except (ValueError, OSError, re.error) as ex:
            problems.append(f"{game.get('title', '?')}: {ex}")
    return problems
