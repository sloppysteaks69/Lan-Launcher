"""Minimal reader for MZ/NE/LE/PE executables: what kind of program it is and which DLLs it imports."""
import struct


def _cstr(f, off: int, limit: int = 256) -> str:
    f.seek(off)
    raw = f.read(limit)
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def read_exe(path: str) -> dict:
    """Returns {kind, arch, imports(set), clr(str|None), gui(bool)}.

    kind: 'pe' (Windows), 'dos' (16-bit DOS or a DOS extender like DOS/4GW), 'win16', 'none'."""
    info = {"kind": "none", "arch": "", "imports": set(), "clr": None, "gui": False}
    try:
        f = open(path, "rb")
    except OSError:
        return info
    with f:
        head = f.read(64)
        if len(head) < 64 or head[:2] not in (b"MZ", b"ZM"):
            if path.lower().endswith(".com"):
                info["kind"] = "dos"
            return info
        info["kind"] = "dos"
        e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        f.seek(0, 2)
        size = f.tell()
        if e_lfanew <= 0 or e_lfanew + 256 > size:
            return info
        f.seek(e_lfanew)
        sig = f.read(4)
        if sig[:2] == b"NE":
            info["kind"] = "win16"
            return info
        if sig[:2] in (b"LE", b"LX"):
            return info  # DOS extender (DOS/4GW etc.)
        if sig != b"PE\0\0":
            return info
        coff = f.read(20)
        machine, nsec, _, _, _, opt_size, _ = struct.unpack("<HHIIIHH", coff)
        opt = f.read(opt_size)
        if len(opt) < 70:
            return info
        info["kind"] = "pe"
        magic = struct.unpack_from("<H", opt, 0)[0]
        pe64 = magic == 0x20B
        info["arch"] = "x64" if pe64 or machine == 0x8664 else "x86"
        info["gui"] = struct.unpack_from("<H", opt, 68)[0] == 2
        nd_off, dd_off = (108, 112) if pe64 else (92, 96)
        if len(opt) < dd_off:
            return info
        ndirs = struct.unpack_from("<I", opt, nd_off)[0]
        dirs = []
        for i in range(min(ndirs, 16)):
            o = dd_off + i * 8
            if o + 8 > len(opt):
                break
            dirs.append(struct.unpack_from("<II", opt, o))
        sections = []
        for _ in range(nsec):
            s = f.read(40)
            if len(s) < 40:
                break
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", s, 8)
            sections.append((va, max(vsize, rawsize), rawptr))

        def rva(r: int):
            for va, sz, ptr in sections:
                if va <= r < va + sz:
                    return r - va + ptr
            return None

        def dlls_from(dir_index: int, desc_size: int, name_off: int) -> None:
            if len(dirs) <= dir_index or not dirs[dir_index][0]:
                return
            off = rva(dirs[dir_index][0])
            if off is None:
                return
            for i in range(512):
                f.seek(off + i * desc_size)
                d = f.read(desc_size)
                if len(d) < desc_size or d == b"\0" * desc_size:
                    break
                name_rva = struct.unpack_from("<I", d, name_off)[0]
                no = rva(name_rva)
                if no is not None:
                    info["imports"].add(_cstr(f, no).lower())

        dlls_from(1, 20, 12)   # import table
        dlls_from(13, 32, 4)   # delay-load imports
        if len(dirs) > 14 and dirs[14][0]:
            off = rva(dirs[14][0])
            if off is not None:
                f.seek(off)
                cor = f.read(16)
                if len(cor) == 16:
                    md = rva(struct.unpack_from("<I", cor, 8)[0])
                    if md is not None:
                        f.seek(md)
                        root = f.read(16)
                        if len(root) == 16 and root[:4] == b"BSJB":
                            ln = struct.unpack_from("<I", root, 12)[0]
                            info["clr"] = f.read(min(ln, 64)).split(b"\0", 1)[0].decode("ascii", "replace")
    return info


def contains_ipx_strings(path: str, max_bytes: int = 48 * 1024 * 1024) -> bool:
    """Cheap hint that a game supports IPX: the exe mentions IPX (as text) somewhere."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return False
    return b"IPX" in data and (b"SPX" in data or b"IPX " in data or b"IPX\0" in data)
