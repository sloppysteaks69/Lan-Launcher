"""Stargate Resistance: record one session's backend traffic to learn the exact XML formats.

    python tools\\sgr_capture.py run      back up the ini files, point the game at this relay, relay + log, restore on Ctrl+C
    python tools\\sgr_capture.py restore  put the backed-up ini files back (if run was killed)
    python tools\\sgr_capture.py status   show where the game's URLs point now

The relay listens on 127.0.0.1:8081 and forwards every request unchanged to the fan backend. Each request and reply
goes to captures\\sgr_<time>.log. Passwords are replaced with *** in the log. Nothing else is changed."""
import http.client
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAME = os.environ.get("SGR_GAME", r"C:\Games\Stargate Resistance")   # set SGR_GAME to your install folder
INIS = [os.path.join(GAME, "SGBGame", "Config", n) for n in ("SGBEngine.ini", "DefaultEngine.ini")]
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(HERE, "captures")
BACKUP = os.path.join(CAP, "ini_backup")
LISTEN = ("127.0.0.1", 8081)
RELAY_BASE = f"http://{LISTEN[0]}:{LISTEN[1]}"
URL_KEYS = ("LoginURL", "ListServersURL", "ObjectiveURL", "DominationPointsURL", "ServerAuthURL", "ServerUpdateURL")
URL_LINE = re.compile(r"^(\s*(?:%s)\s*=\s*)(http://[^/\s]+)(.*)$" % "|".join(URL_KEYS), re.I)
# Server-side calls stay local: a test must not register anything on the fan network.
BLOCK = ("/SGBLogin/ServerUpdate", "/SGBLogin/ServerAuth", "/UpdateDominationPoints")

_log_lock = threading.Lock()
_log_path = ""
_upstream = ""


def read_ini(path: str) -> str:
    with open(path, "r", encoding="latin-1", newline="") as f:
        return f.read()


def write_ini(path: str, text: str) -> None:
    with open(path, "w", encoding="latin-1", newline="") as f:
        f.write(text)


def current_base(path: str) -> str:
    for line in read_ini(path).splitlines():
        m = URL_LINE.match(line)
        if m:
            return m.group(2)
    return ""


def set_base(path: str, base: str) -> int:
    text = read_ini(path)
    out, n = [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        m = URL_LINE.match(body)
        if m:
            line = m.group(1) + base + m.group(3) + line[len(body):]
            n += 1
        out.append(line)
    write_ini(path, "".join(out))
    return n


def backup() -> str:
    """Copies the ini files once. Returns the original backend base URL."""
    os.makedirs(BACKUP, exist_ok=True)
    for p in INIS:
        dst = os.path.join(BACKUP, os.path.basename(p))
        if not os.path.exists(dst):            # never overwrite the first, clean backup
            shutil.copy2(p, dst)
    return current_base(os.path.join(BACKUP, os.path.basename(INIS[0])))


def restore() -> None:
    """Puts the original ini files back, then drops the backup so the next run backs up afresh."""
    pairs = [(os.path.join(BACKUP, os.path.basename(p)), p) for p in INIS]
    if not any(os.path.exists(src) for src, _ in pairs):
        print("No backup to restore.")
        return
    for src, dst in pairs:
        if os.path.exists(src):
            shutil.copyfile(src, dst)
            print("restored", dst)
    if all(current_base(dst) != RELAY_BASE for _, dst in pairs):
        for src, _ in pairs:
            if os.path.exists(src):
                os.remove(src)


def redact(text: str) -> str:
    text = re.sub(r"(?i)(PASSWORD=)[^&\s]*", r"\1***", text)
    return re.sub(r'(?i)(password\s*=\s*")[^"]*', r"\1***", text)


def log(entry: str) -> None:
    with _log_lock, open(_log_path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._relay()

    def do_POST(self):
        self._relay()

    def _relay(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        stamp = time.strftime("%H:%M:%S")
        hdrs = "".join(f"  {k}: {v}\n" for k, v in self.headers.items())
        req_text = redact(body.decode("utf-8", "replace"))
        print(f"{stamp} {self.command} {self.path} ({n} bytes)")
        if any(self.path.startswith(b) for b in BLOCK):
            reply = b""
            status = 200
            log(f"===== {stamp} {self.command} {self.path}  [NOT FORWARDED: server-side call]\n{hdrs}--- request body\n{req_text}\n")
        else:
            host = _upstream.split("//", 1)[1]
            try:
                conn = http.client.HTTPConnection(host, timeout=30)
                fwd = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "content-length")}
                fwd["Host"] = host
                conn.request(self.command, self.path, body=body or None, headers=fwd)
                r = conn.getresponse()
                reply = r.read()
                status = r.status
                rh = "".join(f"  {k}: {v}\n" for k, v in r.getheaders())
                conn.close()
            except OSError as e:
                reply, status, rh = b"", 502, f"  (upstream error: {e})\n"
            log(f"===== {stamp} {self.command} {self.path} -> {status}\n{hdrs}--- request body\n{req_text}\n"
                f"--- reply headers\n{rh}--- reply body\n{redact(reply.decode('utf-8', 'replace'))}\n")
        self.send_response(status)
        self.send_header("Content-Type", "text/xml" if reply.lstrip().startswith(b"<") else "text/plain")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def run() -> None:
    global _log_path, _upstream
    _upstream = backup()
    if not _upstream or _upstream.startswith(RELAY_BASE):
        sys.exit("Could not find the original backend URL in the backup. Run 'restore' first.")
    os.makedirs(CAP, exist_ok=True)
    _log_path = os.path.join(CAP, time.strftime("sgr_%Y-%m-%d_%H%M%S.log"))
    httpd = ThreadingHTTPServer(LISTEN, Relay)
    for p in INIS:
        print(f"{p}: {set_base(p, RELAY_BASE)} URLs now point at the relay")
    print(f"\nRelaying {RELAY_BASE} -> {_upstream}\nLogging to {_log_path}\n"
          "Start the game now (run SGBGame.exe directly, not the fan launcher), log in, open the server list,\n"
          "join a match for a minute, then quit the game and press Ctrl+C here.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        restore()
        print(f"\nDone. Log: {_log_path}")


def status() -> None:
    for p in INIS:
        print(f"{p}: {current_base(p)}")
    print("backup present:", os.path.exists(os.path.join(BACKUP, "SGBEngine.ini")))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"run": run, "restore": restore, "status": status}.get(cmd, status)()
