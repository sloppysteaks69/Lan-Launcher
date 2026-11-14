# Third-party components

LAN Party Launcher is built on other people's open-source and freeware work. This file lists every program,
library and service it bundles, downloads or talks to, where each one comes from, its license, and what the
launcher uses it for. Thank you to all of these projects.

The launcher's own code is GPL-3.0 (see [LICENSE](LICENSE)). Each component below keeps its own license.
Full license texts are in [docs/licenses/](docs/licenses/), which also travels inside every release and update.

## Bundled in the release zip

This source repository leaves these out to stay small; the release zip on the Releases page includes them,
unmodified.

### n2n6 (the internet-party VPN): `lanparty/bin/edge.exe`, `lanparty/bin/supernode.exe`
- **What it does here:** the encrypted peer-to-peer VPN behind "Internet party". The host runs `supernode.exe`,
  every player runs `edge.exe`, and LAN broadcasts cross the tunnel so LAN-only games find each other.
- **Version:** n2n6 v2.3, build r287 (commit `ea2f439`), Windows x64 static build. Unmodified.
- **Project:** [lucktu/n2n6](https://github.com/lucktu/n2n6) by lucktu, forked from
  [mxre/n2n](https://github.com/mxre/n2n), which is based on the original [n2n by ntop](https://github.com/ntop/n2n)
  (Luca Deri and contributors).
  Copyright 2007-16 ntop.org, 2017-24 mxre/n2n contributors, 2025-26 lucktu/n2n6 contributors.
- **License:** GNU General Public License v3.0 ([docs/licenses/GPL-3.0.txt](docs/licenses/GPL-3.0.txt)).
- **Source code:** the exact source of the bundled build is
  <https://github.com/lucktu/n2n6/tree/ea2f439> (download:
  <https://github.com/lucktu/n2n6/archive/ea2f439.tar.gz>). A copy is attached to every GitHub Release that ships the binary.

### DOSBox-X (DOS games, with IPX networking): `lanparty/bin/dosbox-x/`
- **What it does here:** runs DOS games; its IPX-over-UDP networking lets DOS multiplayer games play over the LAN
  or the VPN.
- **Version:** 2026.10.01, the official Windows portable package, unmodified. The package also contains its own
  fonts and helper DLL (Sarasa Gothic, WenQuanYi bitmap fonts, Nouveau IBM, InpOut x64), each under the license
  given in the DOSBox-X package.
- **Project:** [DOSBox-X](https://dosbox-x.com) / [joncampbell123/dosbox-x](https://github.com/joncampbell123/dosbox-x)
  by Jonathan Campbell and contributors, a fork of [DOSBox](https://www.dosbox.com).
- **License:** GNU General Public License v2.0 or later (`lanparty/bin/dosbox-x/COPYING`, also
  [docs/licenses/GPL-2.0.txt](docs/licenses/GPL-2.0.txt)).
- **Source code:** <https://github.com/joncampbell123/dosbox-x/tree/dosbox-x-v2026.10.01>. A copy is
  attached to every GitHub Release that ships the binary.

### python-zstandard 0.25.0 (game-transfer compression): `lanparty/vendor/zstandard/`
- **What it does here:** zstd compression of game copies between launchers (level adjusted to the link).
- **Project:** [indygreg/python-zstandard](https://github.com/indygreg/python-zstandard), Copyright (c) 2016
  Gregory Szorc. Prebuilt Windows wheels (32-bit and 64-bit, Python 3.10-3.13) from PyPI, unmodified.
- **License:** BSD 3-Clause ([docs/licenses/python-zstandard-BSD-3-Clause.txt](docs/licenses/python-zstandard-BSD-3-Clause.txt)).
- **Includes:** the [Zstandard library](https://github.com/facebook/zstd), Copyright (c) Meta Platforms, Inc. and
  affiliates, BSD 3-Clause ([docs/licenses/zstd-BSD-3-Clause.txt](docs/licenses/zstd-BSD-3-Clause.txt); zstd is
  dual-licensed BSD or GPLv2, used here under BSD).

### python-lz4 4.4.5 (compression for older launchers): `lanparty/vendor/lz4/`
- **What it does here:** LZ4 compression when the other PC runs launcher 1.9 or 1.10, which have no zstd.
- **Project:** [python-lz4/python-lz4](https://github.com/python-lz4/python-lz4), Copyright (c) 2012-2013 Steeve
  Morin, maintained by Jonathan Underwood. Prebuilt Windows wheels from PyPI, unmodified.
- **License:** BSD 3-Clause ([docs/licenses/python-lz4-BSD-3-Clause.txt](docs/licenses/python-lz4-BSD-3-Clause.txt)).
- **Includes:** the [LZ4 library](https://github.com/lz4/lz4), Copyright (c) 2011-2020 Yann Collet, BSD 2-Clause
  ([docs/licenses/lz4-BSD-2-Clause.txt](docs/licenses/lz4-BSD-2-Clause.txt)).

## Shared between players with minigames

### Godot Engine 4.7.2 (Choice by combat minigames): `minigames/_engine/`
- **What it does here:** the minigames are Godot games. Players who have minigames share one copy of the Godot
  4.7.2 Windows export template, and the launcher sends it to other players along with the minigames.
  It is not in this repository or the plain release zip.
- **Project:** [Godot Engine](https://godotengine.org) / [godotengine/godot](https://github.com/godotengine/godot),
  Copyright (c) 2014-present Godot Engine contributors, (c) 2007-2014 Juan Linietsky, Ariel Manzur.
- **License:** MIT ([docs/licenses/godot-MIT.txt](docs/licenses/godot-MIT.txt)); the engine's own third-party
  parts are listed in [docs/licenses/godot-COPYRIGHT.txt](docs/licenses/godot-COPYRIGHT.txt).

## Downloaded at run time from their publishers (never bundled)

| Component | What it does here | Project / download | License |
|---|---|---|---|
| TAP-Windows6 9.24.7 driver | The virtual network adapter ("LanPartyVPN") that n2n uses | [OpenVPN/tap-windows6](https://github.com/OpenVPN/tap-windows6), installer from build.openvpn.net | GPL-2.0 (with MIT-licensed parts) |
| IPXWrapper (latest release) | IPX/SPX for old Windows games, carried over UDP | [solemnwarning/ipxwrapper](https://github.com/solemnwarning/ipxwrapper) by Daniel Collins | GPL-2.0 |
| ForceBindIP 1.32 | Makes a game use the VPN address during internet parties | [r1ch.net/projects/forcebindip](https://r1ch.net/projects/forcebindip) by Richard Stanway | Freeware |
| Game prerequisites (DirectX 9 June 2010, Visual C++ 2005-2022, .NET, XNA 4.0, OpenAL, PhysX legacy) | Installed on request when a game needs them | Microsoft, OpenAL ([openal.org](https://www.openal.org)), NVIDIA, from their official download pages | Each publisher's redistributable license |

## Other tools used for game compatibility (not part of the launcher)

| Tool | What we used it for | Project | License |
|---|---|---|---|
| XLiveLessNess (XLLN) | Replaces Games for Windows Live in GFWL games (tested with Universe at War) so they find each other on the LAN/VPN; installed into the game folder by hand | [GlitchyScripts/xlivelessness](https://gitlab.com/GlitchyScripts/xlivelessness) by Glitchy Scripts | LGPL-2.1 |

## Online services contacted
- Public IP lookup (so the host's party code carries its internet address): [api.ipify.org](https://www.ipify.org),
  ifconfig.me, icanhazip.com.
- GitHub's API, only to find the latest IPXWrapper release.

## Runtime
The launcher runs on the player's own [Python](https://www.python.org) 3.10-3.13 (PSF License), which is not
bundled.
