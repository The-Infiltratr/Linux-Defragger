#!/usr/bin/env python3
import struct
import sys
from pathlib import Path

p = Path(sys.argv[1])
active = int(sys.argv[2]) if len(sys.argv) > 2 else 0
with p.open("rb") as f:
    boot = f.read(512)
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    data_sector = reserved + fats * fatsz
    cluster_size = bps * spc
    def coff(c): return (data_sector * bps) + (c - 2) * cluster_size
    f.seek(coff(2)); root = f.read(cluster_size)
    first = (struct.unpack_from("<H", root, 20)[0] << 16) | struct.unpack_from("<H", root, 26)[0]
    f.seek((reserved + active * fatsz) * bps); fat = f.read(fatsz * bps)
    def fv(c): return struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF
    chain = []
    c = first
    while True:
        chain.append(c)
        n = fv(c)
        if n >= 0x0FFFFFF8: break
        c = n
    assert chain == list(range(first, first + 3)), chain
    payload = bytearray()
    for c in chain:
        f.seek(coff(c)); payload.extend(f.read(cluster_size))
    assert payload == b"A" * 512 + b"B" * 512 + b"C" * 512
    assert fv(5) == 0 and fv(6) == 0 and fv(10) == 0
print(f"verified contiguous chain {chain} and exact payload")
