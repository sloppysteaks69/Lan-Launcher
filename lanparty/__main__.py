"""Entry point: python -m lanparty [--port N] [--data DIR] [--name NAME] [--peer IP:PORT] [--no-browser]."""
import argparse
import os
import sys
import time
import webbrowser

from . import HTTP_PORT, VERSION, peers
from .config import Config, default_data_dir
from .discovery import Discovery
from .library import Library
from .netwrap import NetWrap
from .peers import PeerSync
from .server import App, serve
from .transfer import Downloads
from .vote import Votes
from .vpn import recover_game_network, restore_game_network


def main() -> None:
    ap = argparse.ArgumentParser(description="LAN Party Launcher")
    ap.add_argument("--port", type=int, default=HTTP_PORT)
    ap.add_argument("--data", default=default_data_dir())
    ap.add_argument("--name")
    ap.add_argument("--folder", action="append", default=[], help="add a games folder")
    ap.add_argument("--peer", action="append", default=[], help="static peer ip:port")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-discovery", action="store_true",
                    help="no LAN broadcast: only talk to --peer addresses (tests; keeps them off the real LAN)")
    ap.add_argument("--party-master", action="store_true", help="make this PC the Party Master (saved in settings)")
    ap.add_argument("--make-release", action="store_true",
                    help=f"build releases\\LanPartyLauncher_v{VERSION}.zip and exit")
    ap.add_argument("--wait-port", type=float, default=0, help="keep trying the port this many seconds (restart)")
    args = ap.parse_args()

    if args.make_release:
        from .update import make_release, release_files
        out = make_release()
        print(f"Release v{VERSION}: {out} ({len(release_files())} files, {os.path.getsize(out)} bytes)")
        return

    cfg = Config(args.data)
    if args.name:
        cfg.set("name", args.name)
    for f in args.folder:
        f = os.path.normpath(f)
        if os.path.isdir(f) and f not in cfg["folders"]:
            cfg.set("folders", cfg["folders"] + [f])
    for p in args.peer:
        if p not in cfg["static_peers"]:
            cfg.set("static_peers", cfg["static_peers"] + [p])
    if args.party_master:
        cfg.set("party_master", True)

    library = Library(cfg)
    discovery = Discovery(cfg, args.port)
    votes = Votes(cfg)
    sync = PeerSync(cfg, discovery, library, votes)
    downloads = Downloads(cfg, discovery, library, sync)
    net = NetWrap(cfg)
    app = App(cfg, library, discovery, sync, votes, downloads, net)
    discovery.lib_stamp = lambda: max(library.last_scan, library.media.last_scan)   # games or media changed
    discovery.party = peers.current_party   # so a PC outside our party doesn't send us unsigned requests
    # After an update restart, take over the internet party the old launcher handed over (handoff.py). Otherwise,
    # undo a metric/rule left by a crashed or closed launcher.
    if not app.resume_party():
        recover_game_network(cfg.data_dir, app.vpn.log)

    give_up = time.time() + max(0.0, args.wait_port)   # after an update the old launcher is still closing
    while True:
        try:
            serve(app, args.port)
            break
        except OSError as e:
            if time.time() < give_up:
                time.sleep(0.5)
                continue
            print(f"Port {args.port} is busy ({e}). Is the launcher already running?")
            sys.exit(1)
    discovery.start(broadcast=not args.no_discovery)
    sync.start()
    library.rescan_async()
    library.media.rescan_async()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"LAN Party Launcher {VERSION} running as '{cfg['name']}' - open {url}")
    print(f"Settings: {cfg.path}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        # clean up VPN on exit
        if app.vpn.is_active():
            print("Stopping VPN...")
            app.vpn.stop()
        restore_game_network(cfg.data_dir, app.vpn.log)   # no-op unless something is left to undo
        print("Bye.")


if __name__ == "__main__":
    main()
