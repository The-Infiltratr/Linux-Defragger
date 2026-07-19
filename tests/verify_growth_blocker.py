#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify blocker evacuation and canonical 10 percent Growth Defrag placement.

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
        high = struct.unpack_from("<H", root, offset + 20)[0]
        low = struct.unpack_from("<H", root, offset + 26)[0]
        return (high << 16) | low

    def chain(first: int) -> list[int]:
        result = []
        c = first
        while True:
            result.append(c)
            n = fv(c)
            if n >= 0x0FFFFFF8:
                return result
            c = n

    expected_chains = ([3, 4], [6, 7], [9, 10], [12, 13])
    expected_payloads = []
    for file_index in range(4):
        expected_payloads.append(
            b"".join(bytes([0x41 + file_index * 4 + cluster_index]) * cluster_size
                     for cluster_index in range(2))
        )

    for index, expected in enumerate(expected_chains):
        actual = chain(first_at(index * 32))
        assert actual == expected, (index, actual, expected)
        payload = bytearray()
        for cluster in actual:
            f.seek(coff(cluster)); payload.extend(f.read(cluster_size))
        assert payload == expected_payloads[index], (index, payload[:8])

    for gap in (5, 8, 11, 14):
        assert fv(gap) == 0, (gap, fv(gap))

print("verified Growth Defrag blocker evacuation and canonical reserve layout")
