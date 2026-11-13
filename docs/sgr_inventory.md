# Stargate Resistance: inventory for a LAN-only backend

Install looked at (read-only): `<games folder>\Stargate Resistance` (633 files, 3.2 GB). 2026-11-07.

## What's there
- Unreal Engine 3 game, 32-bit: `Binaries\Win32\SGBGame.exe` (38 MB, dated 2012-04-14).
- Online code: CME/FireSky's own `OnlineSubsystemFiresky` (C++ in the exe, script in
  `SGBGame\CookedPC\OnlineSubsystemFiresky.u`, which is compressed). GameSpy and Live subsystems ship but aren't used.
- **Dedicated server is built in.** `Binaries\Win32\Server.bat` runs
  `SGBGame.exe server <map>?game=<GameInfo> -SERVERNAME=.. -MAPNAME=.. -DEDICATED=true -CONNECTIONSTRING=<ip>:7777 -PORT=7777`.
  Four maps/modes are listed there (CTF, two TDM, Capture Points). Match traffic is standard UE3 on UDP 7777.
- Anti-cheat: AhnLab HackShield (`Binaries\Win32\HShield\`). It evidently doesn't block play with the fan setup.
- Fan revival files (2026-06-09): `SGR Launcher.exe` ("stargateresistance.us", v1.0.0.4), `launch\*.txt`,
  `r529-sgw.rar` (not opened), and edited ini files. The exe itself looks original.
- Prereqs it ships: DirectX June 2010, VC++ (x86), .NET 3.5, PhysX 8.10 (the launcher's prereq scan will find these).

## The backend is plain HTTP + XML
All six service URLs are ini settings (`[IpDrv.TcpNetDriver]` in `SGBGame\Config\SGBEngine.ini` lines 330-335,
and `DefaultEngine.ini` lines 91-96). The fan setup only changed them, from CME's hosts to
`http://sgaccount.stargateresistance.us:8081/...`. So **we can point the game at a LAN server by editing the ini**:
no hosts-file trick, no exe patch.

| Setting | Path | Who calls it | What it does |
|---|---|---|---|
| LoginURL | `/SGBLogin/UserAuth` | client | Login. Form POST `USER, PASSWORD, SKU=SGR, Digest=1`. Reply root `SGBLoginSuccess` (SessionKey; AccountInfo: AccountId, DisplayName, ExpireDate, BuildVersion) or `SGBLoginError`. Logout sends `USERID`. |
| ListServersURL | `/SGBLogin/ServerList` | client | Sends `SESSIONKEY`. Reply `ServerList` of `Server` (ServerName, ServerConnectionString, MapName, SeriesName, GameType, SGCPop, SLPop). |
| ServerUpdateURL | `/SGBLogin/ServerUpdate` | dedicated server | Heartbeat XML `ServerUpdate/Server` with name, connection string, series, map, game type, build version, team populations. Reply `ServerCommands` (optional commands: broadcast message, kick player, graceful/hard shutdown). **This is how servers get into the list.** |
| ServerAuthURL | `/SGBLogin/ServerAuth` | dedicated server | Checks a joining player's SessionKey. Reply `ServerAuth` (SessionKey; AccountInfo: AccountId, DisplayName). Fail = "Client failed auth, closing connection". |
| DominationPointsURL | `/SGBLogin/GetDominationPoints`, `/UpdateDominationPoints` | both | Galaxy-war score per planet: PlanetName, SGCPoints, SLPoints (namespace `.../xml/sgblogin`). |
| ObjectiveURL | `/RegistrationWS/AccountAchievementGet`, `/AccountObjectiveGet`, `/AccountObjectiveUpdate`, plus GameObjectiveGet, GameObjectiveRelationshipGet, GameAchievementGet | client | Achievements and objectives, XML `Registration/Service/<Call>/Request` -> `Response` lists. |

Element and attribute **names** are all in the exe. Not yet known: exact nesting (attributes vs child elements in
each reply), which replies are required to reach the lobby, and how SessionKey is passed to ServerAuth.

## LAN-only design
- A launcher plug-in on the party host serves those paths on TCP 8081, LAN/party addresses only.
- Login accepts any name; the password is ignored (it's a LAN). DisplayName = the name typed (or launcher name).
- The server list = dedicated servers that sent a ServerUpdate in the last ~30 s.
- ServerAuth approves any SessionKey the plug-in issued this party.
- Achievements/objectives/domination points: kept in a JSON file in the host's launcher data, or empty to start.
- "Host for everyone" starts `SGBGame.exe server ...` with `-CONNECTIONSTRING=<host LAN ip>:7777`, and every PC's
  ini URLs are switched to `http://<host ip>:8081/...` (backed up, restored after; the fan URLs come back).

## Next steps
1. Pin the exact XML shapes. Fastest: run the game once against the live fan server through a logging proxy in
   the launcher (passwords redacted in the log). Fallback: read the parser in Ghidra.
2. Write the plug-in, test login -> list -> join on this PC with a local dedicated server.
3. Test with two PCs.
