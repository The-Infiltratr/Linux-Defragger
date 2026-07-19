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

out = Path(sys.argv[1] if len(sys.argv) > 1 else "compact-tree.img")
bps = 512
spc = 1
reserved = 32
fats = 2
total_sectors = 131072
root_cluster = 20
volume_id = 0xCAFEBABE

fatsz = 1
while True:
    clusters = (total_sectors - reserved - fats * fatsz) // spc
    needed = ((clusters + 2) * 4 + bps - 1) // bps
    if needed <= fatsz:
        break
    fatsz = needed

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
struct.pack_into("<I", boot, 32, total_sectors)
struct.pack_into("<I", boot, 36, fatsz)
struct.pack_into("<I", boot, 44, root_cluster)
struct.pack_into("<H", boot, 48, 1)
struct.pack_into("<H", boot, 50, 6)
boot[64] = 0x80
boot[66] = 0x29
struct.pack_into("<I", boot, 67, volume_id)
boot[71:82] = b"COMPACTTEST"
boot[82:90] = b"FAT32   "
boot[510:512] = b"\x55\xAA"

fsinfo = bytearray(bps)
struct.pack_into("<I", fsinfo, 0, 0x41615252)
struct.pack_into("<I", fsinfo, 484, 0x61417272)
struct.pack_into("<I", fsinfo, 488, 0xFFFFFFFF)
struct.pack_into("<I", fsinfo, 492, 2)
struct.pack_into("<I", fsinfo, 508, 0xAA550000)

fat = bytearray(fatsz * bps)
def setfat(cluster, value):
    struct.pack_into("<I", fat, cluster * 4, value)

setfat(0, 0x0FFFFFF8)
setfat(1, 0x0FFFFFFF)
setfat(20, 0x0FFFFFFF)
setfat(30, 0x0FFFFFFF)
setfat(40, 0x0FFFFFFF)

data_sector = reserved + fats * fatsz
def cluster_offset(cluster):
    return (data_sector + (cluster - 2) * spc) * bps

def entry(name, attr, first, size=0):
    e = bytearray(32)
    e[0:11] = name
    e[11] = attr
    struct.pack_into("<H", e, 20, (first >> 16) & 0xFFFF)
    struct.pack_into("<H", e, 26, first & 0xFFFF)
    struct.pack_into("<I", e, 28, size)
    return e

root = bytearray(bps)
root[0:32] = entry(b"SUBDIR     ", 0x10, 30)
root[32] = 0

subdir = bytearray(bps)
subdir[0:32] = entry(b".          ", 0x10, 30)
subdir[32:64] = entry(b"..         ", 0x10, 20)
subdir[64:96] = entry(b"FILE    BIN", 0x20, 40, bps)
subdir[96] = 0

with out.open("r+b") as f:
    f.seek(0)
    f.write(boot)
    f.seek(bps)
    f.write(fsinfo)
    f.seek(6 * bps)
    f.write(boot)
    f.seek(7 * bps)
    f.write(fsinfo)
    for copy in range(fats):
        f.seek((reserved + copy * fatsz) * bps)
        f.write(fat)
    f.seek(cluster_offset(20))
    f.write(root)
    f.seek(cluster_offset(30))
    f.write(subdir)
    f.seek(cluster_offset(40))
    f.write(b"Z" * bps)

print(out)
