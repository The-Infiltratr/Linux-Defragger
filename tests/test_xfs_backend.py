#!/usr/bin/python3
from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
from backends.xfs import XfsBackend

BLOCK = 4096
SECTOR = 512
DBLOCKS = 16384
AGBLOCKS = 16384
INODE = 512
INOPBLOCK = 8


def bmbt(startoff: int, startblock: int, length: int, unwritten: bool = False) -> bytes:
    l0 = ((1 if unwritten else 0) << 63) | (startoff << 9) | ((startblock >> 43) & 0x1FF)
    l1 = ((startblock & ((1 << 43) - 1)) << 21) | length
    return struct.pack(">QQ", l0, l1)


def dinode(mode: int, extents: list[tuple[int, int, int]]) -> bytes:
    out = bytearray(INODE)
    struct.pack_into(">HH", out, 0, 0x494E, mode)
    out[4] = 3
    out[5] = 2
    struct.pack_into(">I", out, 16, 1)
    struct.pack_into(">Q", out, 56, sum(length for _logical, _physical, length in extents) * BLOCK)
    struct.pack_into(">Q", out, 64, sum(length for _logical, _physical, length in extents))
    struct.pack_into(">I", out, 76, len(extents))
    for index, (logical, physical, length) in enumerate(extents):
        out[176 + index * 16:176 + (index + 1) * 16] = bmbt(logical, physical, length)
    return bytes(out)


def btree_dinode(mode: int, nextents: int, child_fsb: int) -> bytes:
    out = bytearray(INODE)
    struct.pack_into(">HH", out, 0, 0x494E, mode)
    out[4] = 3
    out[5] = 3
    struct.pack_into(">I", out, 16, 1)
    struct.pack_into(">Q", out, 56, nextents * BLOCK)
    struct.pack_into(">Q", out, 64, nextents + 1)
    struct.pack_into(">I", out, 76, nextents)
    fork_size = INODE - 176
    maxrecs = (fork_size - 4) // 16
    struct.pack_into(">HH", out, 176, 1, 1)
    struct.pack_into(">Q", out, 180, 0)
    struct.pack_into(">Q", out, 176 + 4 + maxrecs * 8, child_fsb)
    return bytes(out)


def make_image(path: Path) -> None:
    image = bytearray(DBLOCKS * BLOCK)
    sb = bytearray(SECTOR)
    sb[:4] = b"XFSB"
    struct.pack_into(">I", sb, 4, BLOCK)
    struct.pack_into(">Q", sb, 8, DBLOCKS)
    struct.pack_into(">I", sb, 84, AGBLOCKS)
    struct.pack_into(">I", sb, 88, 1)
    struct.pack_into(">H", sb, 100, 5)
    struct.pack_into(">H", sb, 102, SECTOR)
    struct.pack_into(">H", sb, 104, INODE)
    struct.pack_into(">H", sb, 106, INOPBLOCK)
    sb[120] = 12
    sb[121] = 9
    sb[122] = 9
    sb[123] = 3
    sb[124] = 14
    struct.pack_into(">Q", sb, 128, 64)
    struct.pack_into(">Q", sb, 136, 61)
    struct.pack_into(">I", sb, 216, 1 << 1)
    image[:SECTOR] = sb

    free = [(16, 100), (101, 102), (103, 110), (112, DBLOCKS)]
    free_count = sum(end - start for start, end in free)
    struct.pack_into(">Q", sb, 144, free_count)
    image[:SECTOR] = sb

    agf = bytearray(SECTOR)
    agf[:4] = b"XAGF"
    struct.pack_into(">III", agf, 4, 1, 0, AGBLOCKS)
    struct.pack_into(">I", agf, 16, 4)
    struct.pack_into(">I", agf, 28, 1)
    struct.pack_into(">I", agf, 52, free_count)
    struct.pack_into(">I", agf, 56, max(end - start for start, end in free))
    image[SECTOR:2 * SECTOR] = agf

    agi = bytearray(SECTOR)
    agi[:4] = b"XAGI"
    struct.pack_into(">III", agi, 4, 1, 0, AGBLOCKS)
    struct.pack_into(">I", agi, 16, 3)
    struct.pack_into(">I", agi, 20, 5)
    struct.pack_into(">I", agi, 24, 1)
    struct.pack_into(">I", agi, 28, 61)
    image[2 * SECTOR:3 * SECTOR] = agi

    # Exercise both internal and leaf forms of the short-pointer B+trees.
    bnobt = bytearray(BLOCK)
    bnobt[:4] = b"AB3B"
    struct.pack_into(">HH", bnobt, 4, 1, 2)
    struct.pack_into(">II", bnobt, 56, free[0][0], free[2][0])
    alloc_ptr_base = 56 + ((BLOCK - 56) // 12) * 8
    struct.pack_into(">II", bnobt, alloc_ptr_base, 6, 7)
    image[4 * BLOCK:5 * BLOCK] = bnobt
    for blockno, records in ((6, free[:2]), (7, free[2:])):
        leaf = bytearray(BLOCK)
        leaf[:4] = b"AB3B"
        struct.pack_into(">HH", leaf, 4, 0, len(records))
        for index, (start, end) in enumerate(records):
            struct.pack_into(">II", leaf, 56 + index * 8, start, end - start)
        image[blockno * BLOCK:(blockno + 1) * BLOCK] = leaf

    inobt = bytearray(BLOCK)
    inobt[:4] = b"IAB3"
    struct.pack_into(">HH", inobt, 4, 1, 1)
    struct.pack_into(">I", inobt, 56, 64)
    ino_ptr_base = 56 + ((BLOCK - 56) // 8) * 4
    struct.pack_into(">I", inobt, ino_ptr_base, 9)
    image[5 * BLOCK:6 * BLOCK] = inobt
    ino_leaf = bytearray(BLOCK)
    ino_leaf[:4] = b"IAB3"
    struct.pack_into(">HH", ino_leaf, 4, 0, 1)
    struct.pack_into(">I", ino_leaf, 56, 64)
    struct.pack_into(">HBBQ", ino_leaf, 60, 0, 64, 61, ((1 << 64) - 1) ^ 7)
    image[9 * BLOCK:10 * BLOCK] = ino_leaf


    inode_block = bytearray(BLOCK)
    inode_block[0:INODE] = dinode(0o100644, [(0, 100, 1), (1, 102, 1)])
    inode_block[INODE:2 * INODE] = dinode(0o040755, [(0, 110, 2)])
    inode_block[2 * INODE:3 * INODE] = btree_dinode(0o100644, 2, 10)
    image[8 * BLOCK:9 * BLOCK] = inode_block
    bmap_leaf = bytearray(BLOCK)
    bmap_leaf[:4] = b"BMA3"
    struct.pack_into(">HH", bmap_leaf, 4, 0, 2)
    bmap_leaf[72:88] = bmbt(0, 12, 1)
    bmap_leaf[88:104] = bmbt(1, 14, 1)
    image[10 * BLOCK:11 * BLOCK] = bmap_leaf
    path.write_bytes(image)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "xfs.img"
        make_image(path)
        result = XfsBackend().map(str(path), 2048)
        assert result["map_accuracy"] == "exact"
        assert result["unknown_bytes"] == 0
        assert result["regular_files"] == 2
        assert result["directories"] == 1
        assert result["fragmented_files"] == 2
        assert result["fragmented_directories"] == 0
        assert result["free_bytes"] == sum(end - start for start, end in
                                             [(16,100),(101,102),(103,110),(112,DBLOCKS)]) * BLOCK
        assert sum(cell["fragmented"] for cell in result["cells"]) > 0
        assert sum(cell["directory"] for cell in result["cells"]) > 0
        print(json.dumps({k: result[k] for k in (
            "used_bytes", "free_bytes", "regular_files", "fragmented_files"
        )}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
