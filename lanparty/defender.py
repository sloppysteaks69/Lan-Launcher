"""Add game folders to Windows Defender's exclusion list so scans don't slow down old games."""
import json
import subprocess
import sys

from .prereqs import run_elevated


def defender_status(folders: list) -> dict:
    """Which of the configured folders are already excluded from Defender scans."""
    if sys.platform != "win32" or not folders:
        return {"excluded": [], "not_excluded": folders}
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-MpPreference).ExclusionPath | ConvertTo-Json"],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000)
        raw = r.stdout.strip()
        if not raw or raw == "null":
            current = []
        else:
            parsed = json.loads(raw)
            current = parsed if isinstance(parsed, list) else [parsed]
    except (OSError, subprocess.SubprocessError, ValueError):
        current = []
    norm = {p.rstrip("\\").lower() for p in current}
    excluded = [f for f in folders if f.rstrip("\\").lower() in norm]
    not_excluded = [f for f in folders if f.rstrip("\\").lower() not in norm]
    return {"excluded": excluded, "not_excluded": not_excluded}


def exclude_folders(cfg, folders: list, note) -> None:
    """Adds each folder to Defender's exclusion list via one elevated PowerShell script."""
    lines = []
    for f in folders:
        q = f.replace("'", "''")
        lines.append(f"Add-MpPreference -ExclusionPath '{q}'")
        lines.append(f"Write-Output 'Excluded: {q}'")
    run_elevated(cfg, lines, lambda m: note("Defender: " + m))
    note("Defender exclusions updated.")
