# Third-party components

The release download (the full zip on the Releases page) bundles the following programs and libraries; this source
repository leaves them out to stay small. Each stays under its own license.

| Component | Where | License |
|---|---|---|
| DOSBox-X | `lanparty/bin/dosbox-x/` | GNU GPL v2 or later (see `lanparty/bin/dosbox-x/COPYING`); source at https://github.com/joncampbell123/dosbox-x |
| n2n (`edge.exe`, `supernode.exe`) | `lanparty/bin/` | GNU GPL v3; source at https://github.com/ntop/n2n |
| python-zstandard 0.25.0 | `lanparty/vendor/zstandard/` | BSD 3-Clause (see `lanparty/vendor/zstandard-0.25.0.dist-info/licenses/`) |
| python-lz4 4.4.5 | `lanparty/vendor/lz4/` | BSD 3-Clause (see `lanparty/vendor/lz4-4.4.5.dist-info/licenses/`) |

Downloaded at run time, never bundled: IPXWrapper, ForceBindIP, the TAP-Windows driver and the game prerequisite
installers (DirectX, Visual C++, .NET and so on), each from its publisher.
