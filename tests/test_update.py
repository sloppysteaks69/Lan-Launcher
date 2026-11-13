"""Unit tests for launcher versions, release zips and applying an update. Nothing outside a temp folder is written.
Run: python tests\\test_update.py"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import update, VERSION  # noqa: E402

fails = []


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


def raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def write(root, rel, data):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def fake_install(root, version):
    write(root, "lanparty/__init__.py", f'VERSION = "{version}"\n')
    write(root, "lanparty/server.py", f"# server {version}\n")
    write(root, "lanparty/web/app.js", f"// ui {version}\n")
    write(root, "lanparty/bin/edge.exe", b"MZ binary")
    write(root, "lanparty/__pycache__/x.cpython-310.pyc", b"pyc")
    write(root, "data/known_games.json", "{}")
    write(root, "docs/lan_json.md", "# doc")
    write(root, "tests/test_x.py", "# test")
    write(root, "tests/notes.txt", "not a py file")
    write(root, "tools/tool.py", "# tool")
    write(root, "README.md", "readme")
    write(root, "run.bat", "@echo off")
    write(root, "backup/old/server.py", "old")
    write(root, "key.txt", "secret")
    write(root, "docs/key.txt", "secret")
    write(root, "old.zip", b"PK")


def bad_zip(path, files, manifest=None, version="9.9.9"):
    with zipfile.ZipFile(path, "w") as z:
        man = {"app": "lanparty", "version": version, "files": {}}
        for n, d in files.items():
            z.writestr(n, d)
            man["files"][n] = {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
        z.writestr("manifest.json", json.dumps(manifest if manifest is not None else man))


# ---- version compare ----
check(update.parse_version("1.10.2") == (1, 10, 2), "parse 1.10.2")
check(update.parse_version("junk") == (0,) and update.parse_version("") == (0,), "bad versions parse as (0,)")
check(update.is_newer("1.10.0", "1.9.9"), "1.10.0 newer than 1.9.9 (numeric, not text)")
check(not update.is_newer("1.1", "1.1.0") and not update.is_newer("1.1.0", "1.1"), "1.1 == 1.1.0")
check(update.is_newer("1.1.1", "1.1") and not update.is_newer("0.9", "1.0.0"), "patch and older")
check(not update.is_newer("abc", "0.1.0") and not update.is_newer(VERSION), "junk / same version never newer")
check(update.parse_version(VERSION) >= (1, 1, 0), f"this launcher is numbered ({VERSION})")

tmp = tempfile.mkdtemp(prefix="lanparty_update_")
try:
    old = os.path.join(tmp, "old")
    new = os.path.join(tmp, "new")
    fake_install(old, "1.0.0")
    fake_install(new, "1.2.0")
    write(new, "lanparty/update_extra.py", "# new module")
    write(new, "docs/examples/Hexen 2/lan.json", "{}")

    # ---- release build ----
    files = update.release_files(new)
    check("lanparty/__init__.py" in files and "lanparty/web/app.js" in files and "run.bat" in files
          and "docs/examples/Hexen 2/lan.json" in files, "release has code, web, docs, root files")
    check(not any(f.startswith(("lanparty/bin/", "backup/")) or "__pycache__" in f or f.endswith((".zip", "key.txt"))
                  or f == "tests/notes.txt" for f in files), f"release leaves out bin, backup, pycache, keys ({files})")
    rz = update.make_release(new, "1.2.0")
    check(rz == os.path.join(new, "releases", "LanPartyLauncher_v1.2.0.zip") and os.path.isfile(rz), "release zip path")
    with zipfile.ZipFile(rz) as z:
        man = json.loads(z.read("manifest.json"))
        ok = all(hashlib.sha256(z.read(n)).hexdigest() == man["files"][n]["sha256"] for n in man["files"])
    check(man["version"] == "1.2.0" and set(man["files"]) == set(files) and ok, "manifest: version and every hash")
    check(update.current_release(new, "1.2.0") == rz, "current_release reuses an up-to-date zip")
    check(update.release_files(new) == update.release_files(new), "release file list is stable")

    # ---- path checks ----
    for bad in ("../evil.py", "lanparty/../../evil.py", "/abs.py", "C:/x.py", "lanparty\\x.py", "lanparty/bin/edge.exe",
                "backup/x.py", "releases/x.zip", "settings.json", "data/settings.json", "key.txt", "evil.exe",
                "windows/system32/x.dll", "lanparty/__pycache__/x.pyc", "lanparty//x.py", "lanparty/./x.py"):
        check(raises(lambda: update.safe_target(bad, old)), f"path refused: {bad}")
    check(update.safe_target("lanparty/server.py", old) == os.path.realpath(os.path.join(old, "lanparty", "server.py")),
          "normal path allowed")

    # ---- verifying a release ----
    z1 = os.path.join(tmp, "trav.zip")
    bad_zip(z1, {"lanparty/__init__.py": b'VERSION = "9.9.9"\n', "../evil.py": b"x"})
    check(raises(lambda: update.check_release(z1, old)), "zip with ../ refused")
    z2 = os.path.join(tmp, "bin.zip")
    bad_zip(z2, {"lanparty/__init__.py": b'VERSION = "9.9.9"\n', "lanparty/bin/edge.exe": b"MZ"})
    check(raises(lambda: update.check_release(z2, old)), "zip writing bin refused")
    z3 = os.path.join(tmp, "hash.zip")
    bad_zip(z3, {"lanparty/__init__.py": b'VERSION = "9.9.9"\n'},
            manifest={"app": "lanparty", "version": "9.9.9", "files": {"lanparty/__init__.py": {"sha256": "0" * 64}}})
    check(raises(lambda: update.check_release(z3, old)), "hash mismatch refused")
    z4 = os.path.join(tmp, "extra.zip")
    with zipfile.ZipFile(z4, "w") as z:
        z.writestr("lanparty/__init__.py", b'VERSION = "9.9.9"\n')
        z.writestr("lanparty/sneaky.py", b"x")
        z.writestr("manifest.json", json.dumps({"app": "lanparty", "version": "9.9.9", "files": {
            "lanparty/__init__.py": {"sha256": hashlib.sha256(b'VERSION = "9.9.9"\n').hexdigest()}}}))
    check(raises(lambda: update.check_release(z4, old)), "file not in the manifest refused")
    z5 = os.path.join(tmp, "noman.zip")
    with zipfile.ZipFile(z5, "w") as z:
        z.writestr("lanparty/__init__.py", b"x")
    check(raises(lambda: update.check_release(z5, old)), "zip without manifest refused")
    z6 = os.path.join(tmp, "notzip.zip")
    write(tmp, "notzip.zip", "hello")
    check(raises(lambda: update.check_release(z6, old)), "non-zip refused")
    before = {r: open(os.path.join(old, *r.split("/")), "rb").read() for r in update.release_files(old)}
    after = {r: open(os.path.join(old, *r.split("/")), "rb").read() for r in update.release_files(old)}
    check(before == after and not os.path.exists(os.path.join(tmp, "evil.py")), "refused zips wrote nothing")

    # ---- applying ----
    old_rel = update.make_release(old, "1.0.0", os.path.join(tmp, "old_release.zip"))
    check(raises(lambda: update.apply_release(old_rel, new)), "older release refused")
    r = update.apply_release(rz, old)
    check(r["version"] == "1.2.0" and update.installed_version(old) == "1.2.0", "update applied (installed v1.2.0)")
    check("lanparty/update_extra.py" in r["written"] and "lanparty/server.py" in r["written"]
          and "data/known_games.json" not in r["written"], f"only changed/new files written ({r['written']})")
    with open(os.path.join(old, "lanparty", "server.py")) as f:
        check(f.read() == "# server 1.2.0\n", "file content replaced")
    bk = r["backup"]
    check(bk and os.path.basename(bk).endswith("_update") and os.path.dirname(bk) == os.path.join(old, "backup"),
          f"backup folder backup\\<ts>_update ({bk})")
    with open(os.path.join(bk, "lanparty", "server.py")) as f:
        check(f.read() == "# server 1.0.0\n", "old file backed up")
    check(open(os.path.join(old, "lanparty", "bin", "edge.exe"), "rb").read() == b"MZ binary"
          and open(os.path.join(old, "key.txt")).read() == "secret", "bin and key.txt untouched")
    check(raises(lambda: update.apply_release(rz, old)), "same version again refused")

    # ---- restart command line ----
    a = update.restart_argv(["--port", "47813", "--wait-port", "5", "--name", "Carol"])
    check(a[:3] == [sys.executable, "-m", "lanparty"] and a.count("--wait-port") == 1 and "--no-browser" in a
          and a[3:7] == ["--port", "47813", "--name", "Carol"], f"restart argv keeps options ({a})")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL_PASS" if not fails else f"FAILED: {len(fails)}")
sys.exit(1 if fails else 0)
