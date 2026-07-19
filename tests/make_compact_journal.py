#!/usr/bin/env python3
import argparse
import struct
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("image", type=Path)
p.add_argument("journal", type=Path)
p.add_argument("--partial-free", action="store_true")
a = p.parse_args()

with a.image.open("r+b") as f:
    boot = bytearray(f.read(512))
    bps = struct.unpack_from("<H", boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from("<I", boot, 36)[0]
    data_sector = reserved + fats * fatsz
    cluster_size = bps * spc
    volume_id = struct.unpack_from("<I", boot, 67)[0]

    def coff(cluster):
        return data_sector * bps + (cluster - 2) * cluster_size

    def fatoff(copy, cluster):
        return (reserved + copy * fatsz) * bps + cluster * 4

    def setfat(cluster, value):
        for copy in range(fats):
            f.seek(fatoff(copy, cluster))
            f.write(struct.pack("<I", value))

    def setfirst(offset, cluster):
        f.seek(offset + 20)
        f.write(struct.pack("<H", (cluster >> 16) & 0xFFFF))
        f.seek(offset + 26)
        f.write(struct.pack("<H", cluster & 0xFFFF))

    pairs = [(40, 2), (30, 3), (20, 4)]
    stage = 0
    if a.partial_free:
        for source, destination in pairs:
            f.seek(coff(source))
            data = f.read(cluster_size)
            f.seek(coff(destination))
            f.write(data)
        for _, destination in pairs:
            setfat(destination, 0x0FFFFFFF)
        setfirst(coff(3) + 64, 2)
        setfirst(coff(4), 3)
        setfirst(coff(3), 3)
        setfirst(coff(3) + 32, 4)
        struct.pack_into("<I", boot, 44, 4)
        f.seek(0)
        f.write(boot)
        f.seek(6 * bps)
        f.write(boot)
        setfat(40, 0)
        stage = 3

lines = [
    "LINUX-DEFRAGGER-COMPACT-JOURNAL-1",
    f"device={a.image}",
    f"volume_id={volume_id:08x}",
    f"stage={stage}",
    "root_old=20",
    "root_new=4",
    "move_count=3",
    "move=40,2,268435455,0",
    "move=30,3,268435455,0",
    "move=20,4,268435455,0",
    "dir_patch_count=4",
    f"dir_patch={coff(3) + 64},40,2",
    f"dir_patch={coff(4)},30,3",
    f"dir_patch={coff(3)},30,3",
    f"dir_patch={coff(3) + 32},20,4",
]
a.journal.write_text("\n".join(lines) + "\n")
