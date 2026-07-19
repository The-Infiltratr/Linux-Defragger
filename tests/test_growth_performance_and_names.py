#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify Growth Defrag batching and FAT long-name UTF-8 reporting.

from __future__ import annotations

import argparse
import struct
import subprocess
import tempfile
from pathlib import Path


def make_many_file_image(path: Path, files: int = 100) -> None:
    bps, spc, reserved, fats, total_sectors = 512, 8, 32, 2, 1_048_576
    fatsz = 1
    while True:
        clusters = (total_sectors - reserved - fats * fatsz) // spc
        needed = ((clusters + 2) * 4 + bps - 1) // bps
        if needed <= fatsz:
            break
        fatsz = needed
    with path.open("wb") as stream:
        stream.truncate(total_sectors * bps)

    boot = bytearray(bps)
    boot[0:3] = b"\xebX\x90"
    boot[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", boot, 11, bps)
    boot[13] = spc
    struct.pack_into("<H", boot, 14, reserved)
    boot[16] = fats
    boot[21] = 0xF8
    struct.pack_into("<I", boot, 32, total_sectors)
    struct.pack_into("<I", boot, 36, fatsz)
    struct.pack_into("<I", boot, 44, 2)
    struct.pack_into("<H", boot, 48, 1)
    struct.pack_into("<H", boot, 50, 6)
    boot[64], boot[66] = 0x80, 0x29
    struct.pack_into("<I", boot, 67, 0x17171717)
    boot[71:82], boot[82:90] = b"BATCHTEST  ", b"FAT32   "
    boot[510:512] = b"\x55\xaa"

    fsinfo = bytearray(bps)
    struct.pack_into("<I", fsinfo, 0, 0x41615252)
    struct.pack_into("<I", fsinfo, 484, 0x61417272)
    struct.pack_into("<I", fsinfo, 488, 0xFFFFFFFF)
    struct.pack_into("<I", fsinfo, 492, 3)
    struct.pack_into("<I", fsinfo, 508, 0xAA550000)

    fat = bytearray(fatsz * bps)
    def set_fat(cluster: int, value: int) -> None:
        struct.pack_into("<I", fat, cluster * 4, value)
    set_fat(0, 0x0FFFFFF8)
    set_fat(1, 0x0FFFFFFF)
    set_fat(2, 0x0FFFFFFF)

    root = bytearray(bps * spc)
    for index in range(files):
        cluster = 1000 + index
        set_fat(cluster, 0x0FFFFFFF)
        offset = index * 32
        root[offset:offset + 11] = f"F{index:07d}".encode()[:8].ljust(8, b" ") + b"BIN"
        root[offset + 11] = 0x20
        struct.pack_into("<H", root, offset + 26, cluster)
        struct.pack_into("<I", root, offset + 28, bps * spc)
    root[files * 32] = 0

    data_sector = reserved + fats * fatsz
    def cluster_offset(cluster: int) -> int:
        return (data_sector + (cluster - 2) * spc) * bps

    with path.open("r+b") as stream:
        stream.seek(0); stream.write(boot)
        stream.seek(bps); stream.write(fsinfo)
        stream.seek(6 * bps); stream.write(boot)
        stream.seek(7 * bps); stream.write(fsinfo)
        for fat_index in range(fats):
            stream.seek((reserved + fat_index * fatsz) * bps)
            stream.write(fat)
        stream.seek(cluster_offset(2)); stream.write(root)
        for index in range(files):
            stream.seek(cluster_offset(1000 + index))
            stream.write(bytes([index % 251]) * bps * spc)


def add_lfn(path: Path) -> None:
    with path.open("r+b") as stream:
        boot = stream.read(512)
        bps = struct.unpack_from("<H", boot, 11)[0]
        spc = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        fats = boot[16]
        fatsz = struct.unpack_from("<I", boot, 36)[0]
        root_offset = (reserved + fats * fatsz) * bps
        stream.seek(root_offset)
        root = bytearray(stream.read(bps * spc))
        short = bytearray(root[0:32])
        second = bytes(root[32:64])
        short[0:11] = b"HAMSON~1MP3"
        checksum = 0
        for byte in short[:11]:
            checksum = ((0x80 if checksum & 1 else 0) + (checksum >> 1) + byte) & 0xFF
        units = list(struct.unpack("<12H", "Häm Song.mp3".encode("utf-16le"))) + [0]
        lfn = bytearray(32)
        lfn[0], lfn[11], lfn[13] = 0x41, 0x0F, checksum
        offsets = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)
        for offset, unit in zip(offsets, units, strict=True):
            struct.pack_into("<H", lfn, offset, unit)
        root[0:32], root[32:64], root[64:96] = lfn, short, second
        root[96] = 0
        stream.seek(root_offset)
        stream.write(root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="linux-defragger-growth-") as temp:
        image = Path(temp) / "batch.img"
        make_many_file_image(image)
        result = subprocess.run(
            [str(args.engine), "growth-defrag", str(image), "--write", "--confirm", str(image),
             "--growth-percent", "10", "--ram-buffer", "256M", "--workers", "2"],
            check=True, capture_output=True, text=True,
        )
        assert "Growth layout batch:" in result.stderr
        transactions_line = next(line for line in result.stdout.splitlines()
                                 if line.startswith("Growth Defrag layout I/O:"))
        transactions = int(transactions_line.rsplit(" ", 2)[1])
        assert transactions < 20, transactions_line
        analysis = subprocess.run([str(args.engine), "analyze", str(image)],
                                  check=True, capture_output=True, text=True).stdout
        assert "Fragmented files:       0" in analysis

        name_image = Path(temp) / "lfn.img"
        make_many_file_image(name_image, files=2)
        add_lfn(name_image)
        listing = subprocess.run([str(args.engine), "analyze", str(name_image), "--list"],
                                 check=True, capture_output=True, text=True).stdout
        assert "Häm Song.mp3" in listing
    print("Growth batching and FAT UTF-8 long-name tests passed")


if __name__ == "__main__":
    main()
