#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Create a FAT32 image for Growth Defrag layout tests.

import struct
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "growth-defrag-fat32.img")
bps = 512
spc = 1
reserved = 32
fats = 2
total_sectors = 131072
root_cluster = 2
volume_id = 0x47524F57

fatsz = 1
while True:
    clusters = (total_sectors - reserved - fats * fatsz) // spc
    needed = ((clusters + 2) * 4 + bps - 1) // bps
    if needed <= fatsz:
        break
    fatsz = needed
cluster_count = (total_sectors - reserved - fats * fatsz) // spc
assert cluster_count >= 65525

with out.open("wb") as f:
    f.truncate(total_sectors * bps)

boot = bytearray(bps)
boot[0:3] = b"\xEB\x58\x90"
boot[3:11] = b"MSWIN4.1"
struct.pack_into("<H", boot, 11, bps)
boot[13] = spc
struct.pack_into("<H", boot, 14, reserved)
boot[16] = fats
boot[21] = 0xF8
struct.pack_into("<H", boot, 24, 63)
struct.pack_into("<H", boot, 26, 255)
struct.pack_into("<I", boot, 32, total_sectors)
struct.pack_into("<I", boot, 36, fatsz)
struct.pack_into("<I", boot, 44, root_cluster)
struct.pack_into("<H", boot, 48, 1)
struct.pack_into("<H", boot, 50, 6)
boot[64] = 0x80
boot[66] = 0x29
struct.pack_into("<I", boot, 67, volume_id)
boot[71:82] = b"GROWTEST   "
boot[82:90] = b"FAT32   "
boot[510:512] = b"\x55\xAA"

fsinfo = bytearray(bps)
struct.pack_into("<I", fsinfo, 0, 0x41615252)
struct.pack_into("<I", fsinfo, 484, 0x61417272)
struct.pack_into("<I", fsinfo, 488, 0xFFFFFFFF)
struct.pack_into("<I", fsinfo, 492, 3)
struct.pack_into("<I", fsinfo, 508, 0xAA550000)

fat = bytearray(fatsz * bps)
def setfat(c: int, v: int) -> None:
    struct.pack_into("<I", fat, c * 4, v)
setfat(0, 0x0FFFFFF8)
setfat(1, 0x0FFFFFFF)
setfat(2, 0x0FFFFFFF)

chain_a = list(range(20, 40, 2))
chain_b = list(range(100, 140, 2))
for chain in (chain_a, chain_b):
    for index, cluster in enumerate(chain):
        setfat(cluster, chain[index + 1] if index + 1 < len(chain) else 0x0FFFFFFF)

fat0_sector = reserved
data_sector = reserved + fats * fatsz
def coff(c: int) -> int:
    return (data_sector + (c - 2) * spc) * bps

root = bytearray(bps)
def dirent(offset: int, name: bytes, first: int, size: int) -> None:
    root[offset:offset + 11] = name
    root[offset + 11] = 0x20
    struct.pack_into("<H", root, offset + 20, (first >> 16) & 0xFFFF)
    struct.pack_into("<H", root, offset + 26, first & 0xFFFF)
    struct.pack_into("<I", root, offset + 28, size)
dirent(0, b"ALPHA   BIN", chain_a[0], len(chain_a) * bps)
dirent(32, b"BRAVO   BIN", chain_b[0], len(chain_b) * bps)
root[64] = 0

with out.open("r+b") as f:
    f.seek(0); f.write(boot)
    f.seek(bps); f.write(fsinfo)
    f.seek(6 * bps); f.write(boot)
    f.seek(7 * bps); f.write(fsinfo)
    for index in range(fats):
        f.seek((fat0_sector + index * fatsz) * bps)
        f.write(fat)
    f.seek(coff(2)); f.write(root)
    for index, cluster in enumerate(chain_a):
        f.seek(coff(cluster)); f.write(bytes([0x41 + index]) * bps)
    for index, cluster in enumerate(chain_b):
        f.seek(coff(cluster)); f.write(bytes([0x61 + index]) * bps)

print(out)
