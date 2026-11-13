# lan.json: per-game LAN settings

Put a `lan.json` in a game's root folder (the folder the launcher sees as one game). Every key is optional.

```json
{
  "game":     "Display name",
  "exe":      "relative\\path\\to\\main.exe",
  "setup":    { "script": "relative\\path\\setup.bat" },
  "host":     { "command": "exe", "args": ["-server", "-name={player_name}"], "then_join": false },
  "join":     { "command": "exe", "args": ["{host_ip}:7777", "-name={player_name}"] },
  "ports":    { "udp": "7777,7778-7780", "tcp": "8081" },
  "firewall": ["netsh advfirewall firewall add rule name=\"My game\" dir=in action=allow protocol=UDP localport=7777"],
  "players":  { "min": 2, "max": 8 }
}
```

| Key | What the launcher does |
|---|---|
| `game` | Name shown on every PC. Also used to match the same game across players. |
| `exe` | Main program, used for Play (solo) and when `command` is `"exe"` or empty. |
| `setup.script` | Run once per PC before the game can start (.bat, .cmd, .ps1 or .exe; not as admin). The card shows "Run one-time setup" until it exits with code 0. Edit the path and it runs again. |
| `host` | Command line for "Host for everyone". `then_join: true` also starts a normal client 4 s later that joins `127.0.0.1` (for games whose host is a dedicated server). |
| `join` | Command line every other player gets from the Join button. |
| `ports` | Inbound allow rules added by Network → Open firewall. Comma list, ranges allowed. |
| `firewall` | Extra commands for the same admin step. Only plain `netsh advfirewall firewall add/delete/set rule ...` lines run; anything else is skipped and shown in red on the Network tab. |
| `players` | Shown on the card; hosting warns when too few or too many players are online. |

## Writing your name into the game's settings (`name`)
For games that can't take a name on the command line. Before every Play, Host or Join, the launcher writes your
launcher name into the game's own settings. Add any of these to a `"name"` list:

```json
"name": [
  { "ini":  "{documents}\\My Games\\Game\\player.ini", "section": "Player", "key": "Name", "value": "{player_name}" },
  { "text": "baseq3\\q3config.cfg", "pattern": "^seta name .*$", "line": "seta name \"{player_name}\"" },
  { "registry": "HKCU\\Software\\Vendor\\Game", "value": "PlayerName", "data": "{player_name}", "type": "string" }
]
```

| Kind | What it does |
|---|---|
| `ini` | Sets `key=value` in `[section]`, adding the key or section if missing. Everything else in the file is kept. |
| `text` | Replaces the first line matching the regex `pattern` with `line`, or appends `line` if none matches. |
| `registry` | Sets a value (`type` `string` or `dword`) under `HKCU\Software\...`. |

- Paths are relative to the game folder. To reach the user's folders, start with `{documents}`, `{appdata}` or
  `{localappdata}`. A path can't climb out of its folder with `..`.
- Only config-type files: `.ini .cfg .con .conf .txt .xml .json .properties .lua .sav`.
- `value` / `line` / `data` must contain `{player_name}` or `{my_ip}`. That stops a `lan.json` (perhaps from a game
  copied from someone else) from putting anything else into your settings. `{my_ip}` is this PC's address (your
  VPN IP in an internet party, else your LAN IP), always a plain IPv4. It's for games that pick the wrong network
  adapter and have a setting to override it. Example, Age of Empires 3, which otherwise announces the Wi-Fi
  address instead of the VPN one, so joins fail:
  `{ "text": "{documents}\\My Games\\Age of Empires 3\\Startup\\user.cfg", "pattern": "^OverrideAddress=.*$", "line": "OverrideAddress=\"{my_ip}\"" }`
  (whole file: `docs\examples\Age of Empires 3\lan.json`). It's rewritten on every Play/Host/Join, so a new VPN
  address each party is no problem.
- File encoding (UTF-16, UTF-8 with BOM, ANSI) and line endings are kept. The first time a file is changed, the
  original is saved next to it as `<file>.lanparty-bak`.
- If an entry can't be applied, the game still starts and the launcher shows a "Name not set" message.

## Program and extra arguments (`exe`, `args`)
`"exe"` picks the program to start (a path inside the game folder). `"args"` adds command-line arguments for
Launch (and for Host/Join when the game has no `host`/`join` command). The gear on a game's card sets both. They're
saved for that PC only (settings `game_overrides`), or, with "Share these with other players", written into the
game's lan.json, with the old file backed up as `lan.json.bak-<date_time>` and other keys kept. Order: this PC's
choice, then lan.json, then the launcher's automatic pick.

## Library filters (`genre`, `year`, `players`)
The Library tab filters by genre, players and release year. The values come from `data\game_info.json` for known
games. A game's lan.json can set or override them:

```json
"genre": "RTS",
"year": 2005,
"players": { "min": 2, "max": 8 }
```

`genre` is free text (max 30 chars). `year` is a whole number from 1970 to 2100. A game with no value shows as
"Unknown" in that filter and is left out when a players range is picked.

## ForceBindIP (`force_bind`)
During an internet party, every native Windows game started from the launcher goes through ForceBindIP. It makes
all of the game's network traffic use your VPN address, so it can't pick Wi-Fi by mistake. The launcher fetches
the pinned ForceBindIP 1.32 from r1ch.net the first time (SHA-256 checked, kept in
`%APPDATA%\LanPartyLauncher\tools\forcebindip`), and it needs the Visual C++ 2015-2022 runtime. If either is
missing, the game starts normally and the notes say why. DOS/DOSBox games, `.bat`/`.ps1` commands and IPX games
(IPXWrapper picks its own adapters) are never wrapped, and nothing is wrapped outside a party.

```json
"force_bind": false        // never wrap this game (e.g. it needs the internet, or has anti-cheat)
"force_bind": "delayed"    // ForceBindIP -i: inject after the game's window opens (for games that crash at start)
```

A force-bound game can't reach the internet (its traffic only goes into the VPN). LAN play is fine, but online
CD-key checks, master servers or update checks fail. Turn it off for such a game with `"force_bind": false`, or
on one PC with the checkbox on the game's card (settings `no_force_bind`).

**Placeholders** in `args`: `{host_ip}` (the host's LAN address), `{player_name}` (your launcher name, letters,
digits, spaces and `_-.[]()` only, max 32), `{game_dir}` (the game folder), `{my_ip}` (your own address: your VPN
IP during an internet party, otherwise your LAN IP; for games that need `-ip`, such as Hammer of Thyrion's Hexen 2,
which otherwise picks the wrong network adapter). `{my_ip}` is never empty (last resort `127.0.0.1`). With
`"then_join"`, a host command that uses `{my_ip}` makes the self-join connect to `{my_ip}` instead of `127.0.0.1`.

A 64-bit game that the launcher flags as IPX (IPX strings in its exe) is started without IPXWrapper, which only
works with 32-bit games; 64-bit builds play over TCP/IP.

These are command-line arguments: nothing in the game's exe is changed. Each list item is passed as one argument,
so a name with spaces stays one argument.

**Safety:** `command`, `exe` and `setup.script` must be files inside the game folder, so a game copied from another
player can't point the launcher at programs elsewhere on your PC.

`lan.json` wins over the older `lanparty.json` and over `data\known_games.json`.
Examples: `docs\examples\Stargate Resistance\lan.json`; `docs\examples\Hexen 2\lan.json` (Hammer of Thyrion
1.5.9 `glh2.exe`: `-ip {my_ip}` binds to the right adapter and `-listen 16` raises the 4-player cap);
`docs\examples\Duke Nukem Forever\lan.json` (a listen-server URL with `?MaxPlayers=16`; the game takes up to 32
although its menus stop at 8).
