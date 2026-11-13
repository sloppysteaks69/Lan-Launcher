"""Tests for lanparty\\regfix.py: .reg files in a copied game get their install paths rewritten to this PC.
Run: python tests\\test_regfix.py"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import regfix  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


BODY = ('\r\n[HKEY_LOCAL_MACHINE\\SOFTWARE\\WOW6432Node\\Microsoft\\Microsoft Games\\Age of Empires 3\\1.0]\r\n'
        '"SetupPath"="E:\\\\Old Games\\\\Age of Empires 3\\\\"\r\n'
        '"Exe"="E:\\\\Old Games\\\\Age of Empires 3\\\\age3.exe"\r\n'
        '"Version"="1.0"\r\n')
HEADERS = {"v5": "Windows Registry Editor Version 5.00\r\n", "regedit4": "REGEDIT4\r\n"}
ENCODINGS = {"utf16": "utf-16", "ansi": "cp1252"}     # what regedit writes for each header

tmp = tempfile.mkdtemp(prefix="lp_regfix_")
try:
    for hname, header in HEADERS.items():
        for ename, enc in ENCODINGS.items():
            p = os.path.join(tmp, f"install_{hname}_{ename}.reg")
            with open(p, "w", encoding=enc, newline="") as f:
                f.write(header + BODY)
            text, got_enc = regfix.read_reg(p)
            check(regfix.is_reg_header(text.splitlines()[0]), f"{hname}/{ename}: header recognised")
            for dest in (r"C:\Games\Age of Empires 3", "D:\\Party Games\\Age of Empires 3\\"):
                out = regfix.rewrite_reg(p, dest)
                d2 = dest.rstrip("\\").replace("\\", "\\\\")
                check(f'"SetupPath"="{d2}\\\\"' in out, f"{hname}/{ename} -> {dest}: SetupPath keeps its trailing \\")
                check(f'"Exe"="{d2}\\\\age3.exe"' in out, f"{hname}/{ename} -> {dest}: Exe rewritten")
                check("Old Games" not in out, f"{hname}/{ename} -> {dest}: no old path left")
                check('"Version"="1.0"' in out and out.startswith(header.strip()), f"{hname}/{ename}: rest kept")
            check(regfix.rewrite_reg(p, r"E:\Old Games\Age of Empires 3") == "", f"{hname}/{ename}: same place, no rewrite")

    # a UTF-16 file must not be read as ANSI, and an ANSI file must not turn into UTF-16 garbage
    check(regfix.read_reg(os.path.join(tmp, "install_v5_utf16.reg"))[1] == "utf-16", "BOM file read as UTF-16")
    check(regfix.read_reg(os.path.join(tmp, "install_v5_ansi.reg"))[1] in ("utf-8", "mbcs", "cp1252"),
          "ANSI file not read as UTF-16")

    # not a regedit file
    junk = os.path.join(tmp, "notes.reg")
    with open(junk, "w", encoding="utf-8") as f:
        f.write("just some notes\n" + BODY)
    check(regfix.rewrite_reg(junk, r"C:\Games\Age of Empires 3") == "", "file without a regedit header is skipped")
    check(regfix.is_reg_header("\ufeffWindows Registry Editor Version 5.00"), "header with BOM recognised")
    check(not regfix.is_reg_header("[HKEY_CURRENT_USER\\Software]"), "a key line isn't a header")

    # only the game's root folder is searched
    game = os.path.join(tmp, "Age of Empires 3")
    os.makedirs(os.path.join(game, "sub"))
    shutil.copy(os.path.join(tmp, "install_v5_utf16.reg"), os.path.join(game, "install.reg"))
    shutil.copy(os.path.join(tmp, "install_v5_utf16.reg"), os.path.join(game, "sub", "other.reg"))
    check([os.path.basename(f) for f in regfix.find_reg_files(game)] == ["install.reg"], "root .reg files only")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nALL_PASS")
