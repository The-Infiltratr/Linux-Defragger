#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify FAT Growth Defrag contiguity, payloads and 10 percent gaps.

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
    data_sector = reserved + fats * fatsz
    cluster_size = bps * spc
    def coff(c: int) -> int:
        return data_sector * bps + (c - 2) * cluster_size
    f.seek(reserved * bps)
    fat = f.read(fatsz * bps)
    def fv(c: int) -> int:
        return struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF
    f.seek(coff(2))
    root = f.read(cluster_size)
    def first_at(offset: int) -> int:
        return (struct.unpack_from("<H", root, offset + 20)[0] << 16) | struct.unpack_from("<H", root, offset + 26)[0]
    def chain(first: int) -> list[int]:
        result = []
        c = first
        while True:
            result.append(c)
            n = fv(c)
            if n >= 0x0FFFFFF8:
                return result
            c = n
    a = chain(first_at(0))
    b = chain(first_at(32))
    assert a == list(range(3, 13)), a
    assert fv(13) == 0, fv(13)
    assert b == list(range(14, 34)), b
    assert fv(34) == 0 and fv(35) == 0, (fv(34), fv(35))
    payload_a = bytearray()
    for c in a:
        f.seek(coff(c)); payload_a.extend(f.read(cluster_size))
    payload_b = bytearray()
    for c in b:
        f.seek(coff(c)); payload_b.extend(f.read(cluster_size))
    expected_a = b"".join(bytes([0x41 + i]) * bps for i in range(10))
    expected_b = b"".join(bytes([0x61 + i]) * bps for i in range(20))
    assert payload_a == expected_a
    assert payload_b == expected_b
print("verified contiguous FAT Growth Defrag layout with 10 percent expansion gaps")
