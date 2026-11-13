# Games tested over the internet party (VPN)

What has been played between two launchers over the n2n VPN (10.128.0.x), and what each game needed. "Automatic"
means the launcher does it during every party, for every game; nothing to set up.

## Automatic for every game during a party
- The VPN adapter (LanPartyVPN) gets metric 1, so broadcasts and "my IP" lookups prefer it.
- A firewall rule allows all programs in from the VPN (10.128.0.0/24) while the party is on.
- Windows "block" rules for a library game (made when someone clicked Cancel on "Allow access?") are turned off
  during the party and back on afterwards.
- Games start as the normal desktop user, even when the launcher runs as admin (some crash when elevated).
- From v1.4.0: native Windows games start through ForceBindIP, bound to your VPN address, so they can't pick Wi-Fi
  by mistake. A force-bound game has no internet; turn it off on the game's card if a game needs it or misbehaves.
  The per-game fixes below still apply. See `docs\lan_json.md` ("ForceBindIP").

## Tested

| Game | Works | What it needed | Where |
|---|---|---|---|
| Dark Reign 2 | Yes | Only the automatic fixes above (block rules, normal-user start). | - |
| Blood II: The Chosen | Yes | lan.json join line with the same .rez list and order as the host (LithTech UDP 27888). | `C:\Games\Blood II\lan.json` |
| Hexen 2 (Hammer of Thyrion 1.5.9) | Yes, up to 16 | `-ip {my_ip}` (it picks the wrong adapter otherwise), `-listen 16` (4-player cap). | `docs\examples\Hexen 2\lan.json` |
| Age of Empires 3 (1.12, community patch) | Yes | `OverrideAddress="{my_ip}"` in `Documents\My Games\Age of Empires 3\Startup\user.cfg`, written by the launcher before each start. Without it, it announces the Wi-Fi address and joins fail. | `docs\examples\Age of Empires 3\lan.json` |

Untested so far in the library: Alien vs Predator 2, Beyond All Reason, C&C Generals Zero Hour, Duke Nukem
Forever, Homeworld, Universe at War.

## When a game sees the host but can't join (or isn't seen at all)
Almost always the game picked the wrong network adapter, usually Wi-Fi instead of the VPN. Check:
1. Does the game show your own IP somewhere? If it isn't 10.128.0.x, that's it.
2. Look for a setting or command-line option that sets the IP: `-ip`, `+ip`, `OverrideAddress`, `bindip`,
   "local address". Put it in the game's lan.json with `{my_ip}` (in `args`, or in a `name` entry for a config
   file; see `docs\lan_json.md`).
3. Joining by typing the host's address (10.128.0.1) often works where the LAN list doesn't.

## Adding a game to this list
Play it over the VPN between two PCs, then add a row: game and version, works yes/no/partly, what it needed, and
where its lan.json lives. If it needed a lan.json, put a copy in `docs\examples\<game>\lan.json`.
