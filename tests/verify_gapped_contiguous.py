#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

import struct
import sys
from pathlib import Path

p = Path(sys.argv[1])
file_count = 64
clusters_per_file = 8
with p.open("rb") as f:
    boot = f.read(512)
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    root = struct.unpack_from("<I", boot, 44)[0] & 0x0FFFFFFF
    cluster_size = bps * spc
    data_sector = reserved + fats * fatsz
    def coff(c): return data_sector * bps + (c - 2) * cluster_size
    f.seek(reserved * bps)
    fat = f.read(fatsz * bps)
    def fatv(c): return struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF
    assert root == 2
    f.seek(coff(root))
    entries = [f.read(32) for _ in range(file_count)]
    starts = []
    for fi, e in enumerate(entries):
        first = ((struct.unpack_from("<H", e, 20)[0] << 16) |
                 struct.unpack_from("<H", e, 26)[0]) & 0x0FFFFFFF
        starts.append(first)
        for logical in range(clusters_per_file):
            c = first + logical
            nxt = fatv(c)
            if logical + 1 == clusters_per_file:
                assert nxt >= 0x0FFFFFF8, (fi, logical, nxt)
            else:
                assert nxt == c + 1, (fi, logical, c, nxt)
            f.seek(coff(c))
            assert f.read(2) == bytes([fi, logical]), (fi, logical)
    assert sorted(starts) == list(range(3, 3 + file_count * clusters_per_file, clusters_per_file))
    for c in range(2, 3 + file_count * clusters_per_file - 1):
        assert fatv(c) != 0, c
    assert fatv(3 + file_count * clusters_per_file) == 0
print("verified terminal staging: all originally contiguous files remained contiguous")
