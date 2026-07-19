#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify FAT12/FAT16 Growth Defrag placement and reserved gap.

import struct
import sys
from pathlib import Path

kind = sys.argv[1]
p = Path(sys.argv[2])
with p.open('rb') as f:
    boot = f.read(512)
    bps = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from('<H', boot, 14)[0]
    fats = boot[16]
    root_entries = struct.unpack_from('<H', boot, 17)[0]
    spf = struct.unpack_from('<H', boot, 22)[0]
    root_secs = (root_entries * 32 + bps - 1) // bps
    data_start = reserved + fats * spf + root_secs
    cluster_size = bps * spc
    root_off = (reserved + fats * spf) * bps
    f.seek(root_off)
    entry = f.read(32)
    first = struct.unpack_from('<H', entry, 26)[0]
    f.seek(reserved * bps)
    fat = f.read(spf * bps)

    def fv(c: int) -> int:
        if kind == 'fat12':
            off = c + c // 2
            pair = struct.unpack_from('<H', fat, off)[0]
            return (pair >> 4) & 0xFFF if c & 1 else pair & 0xFFF
        return struct.unpack_from('<H', fat, c * 2)[0]

    eoc = 0xFF8 if kind == 'fat12' else 0xFFF8
    chain = []
    c = first
    while True:
        chain.append(c)
        n = fv(c)
        if n >= eoc:
            break
        c = n
    assert chain == [2, 3, 4], chain
    assert fv(5) == 0, fv(5)

    def coff(cluster: int) -> int:
        return (data_start + (cluster - 2) * spc) * bps

    payload = bytearray()
    for c in chain:
        f.seek(coff(c))
        payload.extend(f.read(cluster_size))
    assert payload == b'A' * cluster_size + b'B' * cluster_size + b'C' * cluster_size
print(f'verified {kind.upper()} Growth Defrag chain and one-cluster expansion gap')
