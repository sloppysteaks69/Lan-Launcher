"""Library > Movies / TV / Music / Books / Misc: two launchers on this PC share media through the game copier.

Alice has a movie (one file) and a TV series (a folder); Bob has an album. Checks: each sees the other's media under
the right category, a copy lands in the category folder (a file straight in, a folder as a folder) with the same
bytes, a category without a folder refuses until one is chosen, path traversal is blocked, and media sharing can be
turned off. Run from the project folder:  python tests\\test_media.py"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lanparty import media  # noqa: E402
from lanparty.hashcache import HashCache  # noqa: E402

fails = []
A, B = 47821, 47822


def check(c, what):
    print(("PASS " if c else "FAIL ") + what)
    if not c:
        fails.append(what)


def call(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-LanParty": "1"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait(pred, secs=30):
    end = time.time() + secs
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def items(port, cat=None):
    return {m["id"]: m for m in call(port, "state")["media"] if cat is None or m["cat"] == cat}


def units(tmp):
    check(media.category_of("media-movies-bigmovie") == "movies", "category_of reads the id")
    check(media.category_of("media-evil-x") == "" and media.category_of("bigmovie") == "", "unknown ids aren't media")
    check(media.make_id("tv", "Show S01!") == "media-tv-shows01", "ids are letters and digits after the category")
    merged = media.merge([], "me", {"p1": [{"id": "media-movies-a", "title": "A", "size": 5},
                                           {"id": "spaceblaster", "title": "game"}, "junk",
                                           {"id": "media-bogus-x", "title": "bad"}],
                                    "gone": [{"id": "media-movies-b"}]}, {"p1"})
    check([m["id"] for m in merged] == ["media-movies-a"] and merged[0]["owners"] == ["p1"] and not merged[0]["have"],
          "merge keeps only valid media ids from players online")
    # hash cache: a single file (a movie) is hashed; a pass asked for while one runs is done right after it
    hc = HashCache(os.path.join(tmp, "hc"))
    os.makedirs(os.path.join(tmp, "hc"), exist_ok=True)
    f1 = os.path.join(tmp, "hc", "one.bin")
    write(f1, b"x" * 1000)
    hc.hash_folders([f1])
    check(hc.get(f1) == hashlib.sha256(b"x" * 1000).hexdigest(), "hash cache hashes a single file")


def main():
    tmp = tempfile.mkdtemp(prefix="lp_media_")
    procs = []
    try:
        units(tmp)
        movie = os.urandom(9 * 1024 * 1024 + 77)              # big: travels in 8 MB chunks
        ep = [os.urandom(300 * 1024 + i) for i in range(3)]     # small: bundles
        song = os.urandom(50 * 1024)
        am, at, bmu = (os.path.join(tmp, x) for x in ("Alice Movies", "Alice TV", "Bob Music"))
        write(os.path.join(am, "Big Movie (1999).mkv"), movie)
        write(os.path.join(am, "desktop.ini"), b"[x]")         # ignored
        for i, d in enumerate(ep):
            write(os.path.join(at, "The Show S01", "Season 1" if i else "", f"ep{i + 1}.mp4"), d)
        write(os.path.join(bmu, "Album A", "01 Song.flac"), song)
        bm = os.path.join(tmp, "Bob Movies")
        os.makedirs(bm)
        for name, port, peer, folders in (("Alice", A, B, {"movies": am, "tv": at}),
                                          ("Bob", B, A, {"music": bmu, "movies": bm})):
            data = os.path.join(tmp, "data" + name)
            os.makedirs(data)
            with open(os.path.join(data, "settings.json"), "w", encoding="utf-8") as f:
                json.dump({"media_folders": folders}, f)
            procs.append(subprocess.Popen([sys.executable, "-m", "lanparty", "--port", str(port), "--name", name,
                                           "--data", data, "--peer", f"127.0.0.1:{peer}", "--no-browser",
                                           "--no-discovery"], cwd=ROOT))
        mid, tid, aid = "media-movies-bigmovie1999mkv", "media-tv-theshows01", "media-music-albuma"
        check(wait(lambda: {mid, tid, aid} <= set(items(B))), "Bob sees Alice's movie and series, and his album")
        bi = items(B)
        check(bi.get(mid, {}).get("single") and bi[mid]["size"] == len(movie) and not bi[mid]["have"],
              "the movie is one file, Bob doesn't have it")
        check(bi.get(tid, {}).get("files") == 3 and not bi[tid].get("single"), "the series is a folder of 3 files")
        check(bi.get(aid, {}).get("have") and bi[aid]["cat"] == "music", "Bob's album is his, under Music")
        check(not any("desktop" in m for m in bi), "desktop.ini is not an item")
        check(all(not str(m.get("id", "")).startswith("media-") for m in call(B, "state")["library"]),
              "media never shows in the games list")
        st = call(B, "state")
        check(st["settings"]["media_folders"]["music"] == bmu and st["settings"]["media_folders"]["tv"] == "",
              "settings show each category's folder")

        # movie: straight into Bob's Movies folder, same bytes
        check(call(B, "download", {"id": mid})["ok"], "Bob starts the movie")
        check(wait(lambda: any(d["id"] == mid and d["state"] == "done" for d in call(B, "state")["downloads"]), 60),
              "movie copied")
        got = os.path.join(bm, "Big Movie (1999).mkv")
        check(os.path.isfile(got) and sha(got) == hashlib.sha256(movie).hexdigest(), "movie lands in Movies, same bytes")
        check(not [f for f in os.listdir(bm) if f != "Big Movie (1999).mkv"], "nothing else left in the Movies folder")
        check(wait(lambda: items(B)[mid]["have"] and len(items(B)[mid]["owners"]) == 2), "Bob now has the movie")

        # TV: no folder on Bob yet -> refused with a hint; then choose one by path and copy the folder
        call(B, "download", {"id": tid})
        check(wait(lambda: "Choose a folder for TV" in next(d for d in call(B, "state")["downloads"]
                                                         if d["id"] == tid)["error"]), "TV refused until a folder is set")
        bt = os.path.join(tmp, "Bob TV")
        os.makedirs(bt)
        check(not call(B, "media/folder", {"cat": "tv", "path": os.path.join(tmp, "nope")})["ok"], "a missing folder is refused")
        check(not call(B, "media/folder", {"cat": "games", "path": bt})["ok"], "an unknown category is refused")
        check(call(B, "media/folder", {"cat": "tv", "path": bt})["ok"], "Bob picks a TV folder")
        check(call(B, "download", {"id": tid})["ok"], "Bob starts the series")
        check(wait(lambda: any(d["id"] == tid and d["state"] == "done" for d in call(B, "state")["downloads"]), 60),
              "series copied")
        ok = all(sha(os.path.join(bt, "The Show S01", "Season 1" if i else "", f"ep{i + 1}.mp4")) ==
                 hashlib.sha256(d).hexdigest() for i, d in enumerate(ep))
        check(ok, "series lands as a folder in TV with every file the same")
        check(call(B, "download", {"id": tid}).get("ok") is False, "no second copy of what you have")

        # Alice gets Bob's album
        check(wait(lambda: aid in items(A)), "Alice sees Bob's album")
        call(A, "download", {"id": aid})
        check(wait(lambda: "Choose a folder for Music" in next(d for d in call(A, "state")["downloads"]
                                                            if d["id"] == aid)["error"]), "Alice has no Music folder yet")

        # path traversal on a media id, and a game id never resolves into media
        for url in (f"/peer/file/{mid}?p=../dataAlice/settings.json", f"/peer/file/{tid}?p=../../dataAlice/settings.json",
                    f"/peer/file/{mid}?p=desktop.ini"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{A}{url}", timeout=5)
                check(False, f"blocked: {url}")
            except urllib.error.HTTPError as e:
                check(e.code == 404, f"blocked: {url}")

        # sharing off: Alice's media disappears from Bob's list and her files are refused
        check(call(A, "settings", {"share_media": False})["ok"], "Alice turns media sharing off")
        check(wait(lambda: tid in items(B) and items(B)[tid]["owners"] == [call(B, "state")["me"]["id"]]),
              "Bob no longer lists Alice as having the series")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{A}/peer/manifest/{mid}", timeout=5)
            check(False, "manifest refused with sharing off")
        except urllib.error.HTTPError as e:
            check(e.code == 403, "manifest refused with sharing off")
        check(call(A, "settings", {"share_media": True})["ok"], "back on")
        check(call(B, "media/folder", {"cat": "tv", "clear": True})["ok"] and
              call(B, "state")["settings"]["media_folders"]["tv"] == "", "a folder can be cleared")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL_PASS" if not fails else f"FAILED: {len(fails)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
