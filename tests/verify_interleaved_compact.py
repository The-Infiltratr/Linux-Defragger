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
file_count = 32
clusters_per_file = 16
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

    def coff(cluster):
        return data_sector * bps + (cluster - 2) * cluster_size

    f.seek(reserved * bps)
    fat = f.read(fatsz * bps)
    def fatv(cluster):
        return struct.unpack_from("<I", fat, cluster * 4)[0] & 0x0FFFFFFF

    assert root == 2, root
    f.seek(coff(root))
    entries = [f.read(32) for _ in range(file_count)]
    for fi, e in enumerate(entries):
        first = ((struct.unpack_from("<H", e, 20)[0] << 16) |
                 struct.unpack_from("<H", e, 26)[0]) & 0x0FFFFFFF
        expected_first = 3 + fi * clusters_per_file
        assert first == expected_first, (fi, first, expected_first)
        for logical in range(clusters_per_file):
            cluster = first + logical
            expected_next = 0x0FFFFFFF if logical + 1 == clusters_per_file else cluster + 1
            actual = fatv(cluster)
            if expected_next >= 0x0FFFFFF8:
                assert actual >= 0x0FFFFFF8, (fi, logical, actual)
            else:
                assert actual == expected_next, (fi, logical, actual, expected_next)
            f.seek(coff(cluster))
            marker = f.read(2)
            assert marker == bytes([fi, logical]), (fi, logical, marker)

    highest = 2 + file_count * clusters_per_file
    for cluster in range(2, highest + 1):
        assert fatv(cluster) != 0, cluster
    assert fatv(highest + 1) == 0

print("verified whole-chain packing: 32 interleaved files became contiguous without gaps")
