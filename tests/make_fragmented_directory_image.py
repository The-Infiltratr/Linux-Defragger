#!/usr/bin/env python3
import struct
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "fragmented-directories.img")
bps = 512
spc = int(sys.argv[2]) if len(sys.argv) > 2 else 1
reserved = 32
fats = 2
total_sectors = 131072 if spc == 1 else 2100000
cluster_size = bps * spc
root_cluster = 20
volume_id = 0xD1A3C700

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
boot[71:82] = b"DIRDEFRAG  "
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
setfat(20, 60)
setfat(60, 0x0FFFFFFF)
setfat(30, 80)
setfat(80, 0x0FFFFFFF)
setfat(70, 0x0FFFFFFF)
setfat(90, 100)
setfat(100, 0x0FFFFFFF)

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

def deleted_cluster():
    c = bytearray(cluster_size)
    for pos in range(0, cluster_size, 32):
        c[pos] = 0xE5
    return c

root_first = deleted_cluster()
root_first[0:32] = entry(b"SUBDIR     ", 0x10, 30)
root_second = bytearray(cluster_size)
root_second[0] = 0

sub_first = deleted_cluster()
sub_first[0:32] = entry(b".          ", 0x10, 30)
sub_first[32:64] = entry(b"..         ", 0x10, 20)
sub_first[64:96] = entry(b"CHILD      ", 0x10, 70)
sub_first[96:128] = entry(b"FILE    BIN", 0x20, 90, 2 * cluster_size)
sub_second = bytearray(cluster_size)
sub_second[0] = 0

child = bytearray(cluster_size)
child[0:32] = entry(b".          ", 0x10, 70)
child[32:64] = entry(b"..         ", 0x10, 30)
child[64] = 0

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
    for cluster, data in [
        (20, root_first), (60, root_second),
        (30, sub_first), (80, sub_second),
        (70, child), (90, b"A" * cluster_size), (100, b"B" * cluster_size),
    ]:
        f.seek(cluster_offset(cluster))
        f.write(data)

print(out)
