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

image = Path(sys.argv[1])
journal = Path(sys.argv[2])
file_count = 2
clusters_per_file = 16
base_cluster = 1000
interleave = 32

with image.open("rb") as f:
    boot = f.read(512)
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    root = struct.unpack_from("<I", boot, 44)[0] & 0x0FFFFFFF
    volume_id = struct.unpack_from("<I", boot, 67)[0]
    cluster_size = bps * spc
    data_sector = reserved + fats * fatsz


def coff(cluster):
    return data_sector * bps + (cluster - 2) * cluster_size

moves = []
patches = []
for fi in range(file_count):
    source_chain = [base_cluster + fi + logical * interleave
                    for logical in range(clusters_per_file)]
    dest_start = 3 + fi * clusters_per_file
    for logical, source in enumerate(source_chain):
        nxt = 0x0FFFFFFF if logical + 1 == clusters_per_file else source_chain[logical + 1]
        pred = 0 if logical == 0 else source_chain[logical - 1]
        moves.append((source, dest_start + logical, nxt, pred))
    patches.append((coff(root) + fi * 32, source_chain[0], dest_start))

lines = [
    "LINUX-DEFRAGGER-COMPACT-JOURNAL-1",
    f"device={image}",
    f"volume_id={volume_id:08x}",
    "stage=0",
    f"root_old={root}",
    f"root_new={root}",
    f"move_count={len(moves)}",
]
lines.extend(f"move={s},{d},{n},{p}" for s, d, n, p in moves)
lines.append(f"dir_patch_count={len(patches)}")
lines.extend(f"dir_patch={o},{old},{new}" for o, old, new in patches)
journal.write_text("\n".join(lines) + "\n")
