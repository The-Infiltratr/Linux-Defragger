#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

import argparse
import struct
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("image", type=Path)
ap.add_argument("--expect-contiguous", type=int, default=32)
a = ap.parse_args()

file_count = 32
clusters_per_file = 16
with a.image.open("rb") as f:
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

    f.seek(coff(root))
    entries = [f.read(32) for _ in range(file_count)]
    claimed = set()
    contiguous = 0
    for fi, e in enumerate(entries):
        first = ((struct.unpack_from("<H", e, 20)[0] << 16) |
                 struct.unpack_from("<H", e, 26)[0]) & 0x0FFFFFFF
        chain = []
        cur = first
        for logical in range(clusters_per_file):
            assert cur >= 2, (fi, logical, cur)
            assert cur not in claimed, ("crosslink", fi, logical, cur)
            claimed.add(cur)
            chain.append(cur)
            f.seek(coff(cur))
            marker = f.read(2)
            assert marker == bytes([fi, logical]), (fi, logical, cur, marker)
            nxt = fatv(cur)
            if logical + 1 == clusters_per_file:
                assert nxt >= 0x0FFFFFF8, (fi, logical, cur, nxt)
            else:
                assert 2 <= nxt < 0x0FFFFFF8, (fi, logical, cur, nxt)
                cur = nxt
        if all(chain[i] + 1 == chain[i + 1] for i in range(len(chain) - 1)):
            contiguous += 1

    assert contiguous == a.expect_contiguous, (contiguous, a.expect_contiguous)

print(f"verified payload and FAT chains; {contiguous} files contiguous")
