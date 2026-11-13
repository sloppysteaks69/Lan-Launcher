"""Imports .reg files from a game's folder, rewriting paths to match the game's current location.

Old-game installers store paths in the Windows registry (install dir, CD path, etc.). When a game
is transferred between PCs via the launcher, those registry entries are missing. A .reg file in the
game's root is detected, its embedded paths are rewritten to the game's actual location, and the
result is imported via an elevated reg.exe call."""
import os
import re
import sys
import tempfile

from .prereqs import run_elevated

IS_WIN = sys.platform == "win32"

# Matches a quoted string value in a .reg file:  "Name"="value"  or  "Name"=hex:...
_VAL_RE = re.compile(r'^"[^"]*"="(.*)"$')
# Matches something that looks like an absolute Windows path (drive letter or UNC)
_PATH_RE = re.compile(r'[A-Za-z]:\\|\\\\')


def find_reg_files(game_path: str) -> list:
    """Return .reg files in the game's root directory (not recursive)."""
    try:
        return sorted(os.path.join(game_path, f) for f in os.listdir(game_path)
                       if f.lower().endswith(".reg") and os.path.isfile(os.path.join(game_path, f)))
    except OSError:
        return []


def _unescape_reg(s: str) -> str:
    """Un-escape a .reg file string value (doubled backslashes → single)."""
    return s.replace("\\\\", "\\")


def _escape_reg(s: str) -> str:
    """Escape a path for a .reg file string value (single backslashes → doubled)."""
    return s.replace("\\", "\\\\")


def _infer_old_root(lines: list, game_folder_name: str) -> str:
    """Try to figure out the original install path from the .reg file contents.

    Strategy: find string values containing the game folder name and extract the prefix.
    For example, if game_folder_name is "Dark Reign 2" and a value is
    "d:\\\\games\\\\untested\\\\Dark Reign 2\\\\dr2.exe", the old root is
    "d:\\games\\untested\\Dark Reign 2".
    """
    folder_lower = game_folder_name.lower()
    candidates = {}
    for line in lines:
        m = _VAL_RE.match(line.strip())
        if not m:
            continue
        val = _unescape_reg(m.group(1))
        idx = val.lower().find(folder_lower)
        if idx < 0 or not _PATH_RE.match(val):
            continue
        root = val[:idx + len(game_folder_name)]
        norm = root.rstrip("\\").lower()
        candidates[norm] = root.rstrip("\\")
    if not candidates:
        return ""
    # pick the most common one
    return max(candidates.values(), key=lambda r: sum(1 for c in candidates if c == r.rstrip("\\").lower()))


def read_reg(reg_path: str) -> tuple:
    """(text, encoding) of a .reg file, or ('', '') if it can't be read. regedit's 'Version 5.00' exports are
    UTF-16 with a BOM; REGEDIT4 files are ANSI. Decoding by the BOM matters: an ANSI file read as UTF-16 doesn't
    fail, it turns into garbage."""
    try:
        with open(reg_path, "rb") as f:
            raw = f.read()
    except OSError:
        return "", ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    elif raw[:3] == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    else:
        try:
            raw.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            enc = "mbcs" if IS_WIN else "cp1252"
    try:
        return raw.decode(enc), enc
    except (UnicodeError, LookupError):
        return "", ""


def is_reg_header(line: str) -> bool:
    """First line of a regedit file: 'Windows Registry Editor Version 5.00' (current) or 'REGEDIT4' (old)."""
    h = line.lstrip("﻿").strip().lower()
    return h.startswith("windows registry editor") or h.startswith("regedit4")


def rewrite_reg(reg_path: str, game_path: str) -> str:
    """Read a .reg file, replace old game paths with the current game_path, return the fixed content.

    Returns empty string if nothing needs rewriting or the file can't be parsed."""
    game_path = game_path.rstrip("\\")
    game_folder = os.path.basename(game_path)
    content, _ = read_reg(reg_path)
    lines = content.splitlines()
    if not lines or not is_reg_header(lines[0]):
        return ""
    old_root = _infer_old_root(lines, game_folder)
    if not old_root:
        return ""
    old_escaped = _escape_reg(old_root)
    new_escaped = _escape_reg(game_path)
    if old_escaped.lower() == new_escaped.lower():
        return ""
    fixed = re.sub(re.escape(old_escaped), lambda m: new_escaped, content, flags=re.IGNORECASE)
    return fixed


def reg_status(game: dict) -> dict:
    """Check whether a game has .reg files and whether they've been imported."""
    path = game.get("path", "")
    files = find_reg_files(path)
    if not files:
        return {"has_reg": False, "files": [], "imported": False}
    return {"has_reg": True, "files": [os.path.basename(f) for f in files], "imported": False}


def import_reg_files(cfg, game: dict, note) -> str:
    """Rewrite and import all .reg files from the game's folder. Returns 'ok' or an error."""
    if not IS_WIN:
        return "Not Windows"
    path = game.get("path", "")
    files = find_reg_files(path)
    if not files:
        return "No .reg files found"
    ps_lines = []
    temp_dir = tempfile.mkdtemp(prefix="lanparty_reg_")
    count = 0
    for reg_file in files:
        fixed = rewrite_reg(reg_file, path)
        if not fixed:
            # no path rewriting needed — import as-is
            q = reg_file.replace("'", "''")
            ps_lines.append(f"& reg import '{q}'")
            ps_lines.append(f"Write-Output 'Imported: {os.path.basename(reg_file)}'")
            count += 1
            continue
        # write the fixed .reg to a temp file and import that
        temp_reg = os.path.join(temp_dir, os.path.basename(reg_file))
        enc = read_reg(reg_file)[1] or "utf-16"
        if enc.startswith("utf-8"):
            enc = "utf-16"           # reg import reads UTF-16 (with BOM) or ANSI; a UTF-8 .reg was hand-made
        try:
            with open(temp_reg, "w", encoding=enc, newline="") as f:
                f.write(fixed)
        except OSError as e:
            note(f"Failed to write temp .reg: {e}")
            continue
        q = temp_reg.replace("'", "''")
        ps_lines.append(f"& reg import '{q}'")
        ps_lines.append(f"Write-Output 'Imported (path-fixed): {os.path.basename(reg_file)}'")
        count += 1
    if not ps_lines:
        return "No .reg files could be processed"
    note(f"Importing {count} registry file(s)...")
    run_elevated(cfg, ps_lines, lambda m: note("Registry: " + m))
    note("Registry import complete.")
    return "ok"
