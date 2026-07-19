#!/usr/bin/env python3
import struct
import sys
from pathlib import Path

p = Path(sys.argv[1])
with p.open("rb") as f:
    boot = f.read(512)
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    root = struct.unpack_from("<I", boot, 44)[0] & 0x0FFFFFFF
    data_sector = reserved + fats * fatsz
    cluster_size = bps * spc

    def coff(cluster):
        return data_sector * bps + (cluster - 2) * cluster_size

    def first(entry):
        return ((struct.unpack_from("<H", entry, 20)[0] << 16) |
                struct.unpack_from("<H", entry, 26)[0]) & 0x0FFFFFFF

    f.seek(reserved * bps)
    fat = f.read(fatsz * bps)
    def fatv(cluster):
        return struct.unpack_from("<I", fat, cluster * 4)[0] & 0x0FFFFFFF

    assert root == 2, root
    assert fatv(2) == 3, fatv(2)
    assert fatv(3) >= 0x0FFFFFF8, fatv(3)

    f.seek(coff(root))
    sub = first(f.read(32))
    assert sub == 4, sub
    assert fatv(4) == 5, fatv(4)
    assert fatv(5) >= 0x0FFFFFF8, fatv(5)

    f.seek(coff(sub))
    dot = f.read(32)
    dotdot = f.read(32)
    child_entry = f.read(32)
    file_entry = f.read(32)
    assert first(dot) == 4, first(dot)
    assert first(dotdot) == 2, first(dotdot)
    child = first(child_entry)
    file_cluster = first(file_entry)
    assert child == 70, child
    assert file_cluster == 6, file_cluster
    assert fatv(6) == 7, fatv(6)
    assert fatv(7) >= 0x0FFFFFF8, fatv(7)

    f.seek(coff(child) + 32)
    child_dotdot = f.read(32)
    assert first(child_dotdot) == 4, first(child_dotdot)

    f.seek(coff(file_cluster))
    payload = f.read(2 * cluster_size)
    assert payload == b"A" * cluster_size + b"B" * cluster_size

    for old in (20, 60, 30, 80, 90, 100):
        assert fatv(old) == 0, (old, fatv(old))

    f.seek(6 * bps)
    backup = f.read(bps)
    assert (struct.unpack_from("<I", backup, 44)[0] & 0x0FFFFFFF) == 2

print("verified root, subdirectory and regular-file defragmentation with directory references")
