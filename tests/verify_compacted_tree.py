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

    f.seek(coff(root))
    sub = first(f.read(32))
    f.seek(coff(sub))
    dot = f.read(32)
    dotdot = f.read(32)
    file_entry = f.read(32)
    file_cluster = first(file_entry)
    f.seek(coff(file_cluster))
    payload = f.read(cluster_size)

    f.seek(reserved * bps)
    fat = f.read(fatsz * bps)
    def fatv(cluster):
        return struct.unpack_from("<I", fat, cluster * 4)[0] & 0x0FFFFFFF

    assert root in (2, 4), root
    assert sub == 3, sub
    assert first(dot) == 3, first(dot)
    assert first(dotdot) == root, (first(dotdot), root)
    assert file_cluster == (4 if root == 2 else 2), (file_cluster, root)
    assert payload == b"Z" * cluster_size
    assert all(fatv(c) >= 0x0FFFFFF8 for c in (2, 3, 4))
    assert all(fatv(c) == 0 for c in (20, 30, 40))

print("verified compacted root, subdirectory references, file entry and payload")
