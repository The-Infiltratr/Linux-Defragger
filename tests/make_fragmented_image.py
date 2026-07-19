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

out = Path(sys.argv[1] if len(sys.argv) > 1 else "fragmented-fat32.img")
bps = 512
spc = 1
reserved = 32
fats = 2
total_sectors = 131072  # 64 MiB
root_cluster = 2
volume_id = 0x1234ABCD

# Choose a FAT large enough for every addressable data cluster.  Some volume
# sizes oscillate by one sector if solved as a naive fixed-point iteration.
fatsz = 1
while True:
    clusters = (total_sectors - reserved - fats * fatsz) // spc
    needed = ((clusters + 2) * 4 + bps - 1) // bps
    if needed <= fatsz:
        break
    fatsz = needed

cluster_count = (total_sectors - reserved - fats * fatsz) // spc
assert cluster_count >= 65525
size = total_sectors * bps
with out.open("wb") as f:
    f.truncate(size)

boot = bytearray(bps)
boot[0:3] = b"\xEB\x58\x90"
boot[3:11] = b"MSWIN4.1"
struct.pack_into("<H", boot, 11, bps)
boot[13] = spc
struct.pack_into("<H", boot, 14, reserved)
boot[16] = fats
struct.pack_into("<H", boot, 17, 0)
struct.pack_into("<H", boot, 19, 0)
boot[21] = 0xF8
struct.pack_into("<H", boot, 22, 0)
struct.pack_into("<H", boot, 24, 63)
struct.pack_into("<H", boot, 26, 255)
struct.pack_into("<I", boot, 28, 0)
struct.pack_into("<I", boot, 32, total_sectors)
struct.pack_into("<I", boot, 36, fatsz)
struct.pack_into("<H", boot, 40, 0)
struct.pack_into("<H", boot, 42, 0)
struct.pack_into("<I", boot, 44, root_cluster)
struct.pack_into("<H", boot, 48, 1)
struct.pack_into("<H", boot, 50, 6)
boot[64] = 0x80
boot[66] = 0x29
struct.pack_into("<I", boot, 67, volume_id)
boot[71:82] = b"DEFRAGTEST "
boot[82:90] = b"FAT32   "
boot[510:512] = b"\x55\xAA"

fsinfo = bytearray(bps)
struct.pack_into("<I", fsinfo, 0, 0x41615252)
struct.pack_into("<I", fsinfo, 484, 0x61417272)
struct.pack_into("<I", fsinfo, 488, 0xFFFFFFFF)
struct.pack_into("<I", fsinfo, 492, 3)
struct.pack_into("<I", fsinfo, 508, 0xAA550000)

fat = bytearray(fatsz * bps)
def setfat(c, v):
    struct.pack_into("<I", fat, c * 4, v)
setfat(0, 0x0FFFFFF8)
setfat(1, 0x0FFFFFFF)
setfat(2, 0x0FFFFFFF)  # root
setfat(5, 10)
setfat(10, 6)
setfat(6, 0x0FFFFFFF)

fat0_sector = reserved
data_sector = reserved + fats * fatsz
def coff(c):
    return (data_sector + (c - 2) * spc) * bps

root = bytearray(bps)
root[0:11] = b"FILE    BIN"
root[11] = 0x20
struct.pack_into("<H", root, 20, 0)
struct.pack_into("<H", root, 26, 5)
struct.pack_into("<I", root, 28, 3 * bps)
root[32] = 0

with out.open("r+b") as f:
    f.seek(0); f.write(boot)
    f.seek(bps); f.write(fsinfo)
    f.seek(6 * bps); f.write(boot)
    f.seek(7 * bps); f.write(fsinfo)
    for i in range(fats):
        f.seek((fat0_sector + i * fatsz) * bps)
        f.write(fat)
    f.seek(coff(2)); f.write(root)
    for cluster, byte in [(5, b"A"), (10, b"B"), (6, b"C")]:
        f.seek(coff(cluster))
        f.write(byte * bps)

print(out)
print(f"clusters={cluster_count} fatsz={fatsz} data_sector={data_sector}")
