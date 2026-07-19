#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify ext2/ext4 read-only fragmentation analysis from inode block maps.

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))

from backends.ext4 import BACKEND

BLOCK_SIZE = 1024
TOTAL_BLOCKS = 4096
TOTAL_INODES = 64
INODES_PER_GROUP = 64
INODE_SIZE = 256
BLOCK_BITMAP = 3
INODE_BITMAP = 4
INODE_TABLE = 5


def _extent_node(extents: list[tuple[int, int, int]], depth: int = 0) -> bytes:
    data = bytearray(BLOCK_SIZE if depth else 60)
    struct.pack_into("<HHHHI", data, 0, 0xF30A, len(extents), (len(data) - 12) // 12, depth, 0)
    for index, (logical, physical, length) in enumerate(extents):
        pos = 12 + index * 12
        if depth:
            struct.pack_into("<IIHH", data, pos, logical, physical & 0xFFFFFFFF,
                             (physical >> 32) & 0xFFFF, 0)
        else:
            struct.pack_into("<IHHI", data, pos, logical, length,
                             (physical >> 32) & 0xFFFF, physical & 0xFFFFFFFF)
    return bytes(data)


def _inode(mode: int, *, extents: list[tuple[int, int, int]] | None = None,
           extent_index: tuple[int, int] | None = None,
           pointers: list[int] | None = None) -> bytes:
    inode = bytearray(INODE_SIZE)
    struct.pack_into("<H", inode, 0, mode)
    struct.pack_into("<H", inode, 26, 1)
    if extents is not None:
        struct.pack_into("<I", inode, 32, 0x00080000)
        inode[40:100] = _extent_node(extents)
    elif extent_index is not None:
        struct.pack_into("<I", inode, 32, 0x00080000)
        logical, block = extent_index
        inode[40:100] = _extent_node([(logical, block, 0)], depth=1)[:60]
    elif pointers is not None:
        values = pointers + [0] * (15 - len(pointers))
        struct.pack_into("<15I", inode, 40, *values[:15])
    return bytes(inode)


def _set_bitmap_bit(bitmap: bytearray, value: int) -> None:
    bitmap[value >> 3] |= 1 << (value & 7)


def _base_image(*, ext4: bool) -> bytearray:
    image = bytearray(TOTAL_BLOCKS * BLOCK_SIZE)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 0, TOTAL_INODES)
    struct.pack_into("<I", sb, 4, TOTAL_BLOCKS)
    struct.pack_into("<I", sb, 20, 1)
    struct.pack_into("<I", sb, 24, 0)
    struct.pack_into("<I", sb, 32, TOTAL_BLOCKS)
    struct.pack_into("<I", sb, 40, INODES_PER_GROUP)
    struct.pack_into("<H", sb, 56, 0xEF53)
    struct.pack_into("<H", sb, 88, INODE_SIZE)
    struct.pack_into("<I", sb, 92, 0)
    struct.pack_into("<I", sb, 96, 0x40 if ext4 else 0)
    struct.pack_into("<I", sb, 100, 0)

    desc = memoryview(image)[2 * BLOCK_SIZE:2 * BLOCK_SIZE + 32]
    struct.pack_into("<III", desc, 0, BLOCK_BITMAP, INODE_BITMAP, INODE_TABLE)

    block_bitmap = bytearray(BLOCK_SIZE)
    for block in range(1, 22):
        _set_bitmap_bit(block_bitmap, block - 1)
    image[BLOCK_BITMAP * BLOCK_SIZE:(BLOCK_BITMAP + 1) * BLOCK_SIZE] = block_bitmap
    return image


def _put_inode(image: bytearray, number: int, inode: bytes) -> None:
    offset = INODE_TABLE * BLOCK_SIZE + (number - 1) * INODE_SIZE
    image[offset:offset + INODE_SIZE] = inode
    inode_bitmap = bytearray(image[INODE_BITMAP * BLOCK_SIZE:(INODE_BITMAP + 1) * BLOCK_SIZE])
    _set_bitmap_bit(inode_bitmap, number - 1)
    image[INODE_BITMAP * BLOCK_SIZE:(INODE_BITMAP + 1) * BLOCK_SIZE] = inode_bitmap


def _mark_blocks(image: bytearray, blocks: list[int]) -> None:
    bitmap = bytearray(image[BLOCK_BITMAP * BLOCK_SIZE:(BLOCK_BITMAP + 1) * BLOCK_SIZE])
    for block in blocks:
        _set_bitmap_bit(bitmap, block - 1)
    image[BLOCK_BITMAP * BLOCK_SIZE:(BLOCK_BITMAP + 1) * BLOCK_SIZE] = bitmap


def make_ext4(path: Path) -> None:
    image = _base_image(ext4=True)
    image[50 * BLOCK_SIZE:51 * BLOCK_SIZE] = _extent_node([(0, 200, 2), (2, 300, 2)])
    _put_inode(image, 2, _inode(0x41ED, extents=[(0, 100, 1)]))
    _put_inode(image, 12, _inode(0x81A4, extent_index=(0, 50)))
    _put_inode(image, 13, _inode(0x81A4, extents=[(0, 400, 4)]))
    _put_inode(image, 14, _inode(0x41ED, extents=[(0, 500, 1), (1, 600, 1)]))
    _put_inode(image, 15, _inode(0x81A4, extents=[]))
    _mark_blocks(image, [50, 100, 200, 201, 300, 301, 400, 401, 402, 403, 500, 600])
    path.write_bytes(image)


def make_ext2(path: Path) -> None:
    image = _base_image(ext4=False)
    indirect = bytearray(BLOCK_SIZE)
    struct.pack_into("<III", indirect, 0, 800, 900, 901)
    image[60 * BLOCK_SIZE:61 * BLOCK_SIZE] = indirect
    pointers = [700, 701] + [0] * 10 + [60]
    _put_inode(image, 2, _inode(0x41ED, pointers=[100]))
    _put_inode(image, 12, _inode(0x81A4, pointers=pointers))
    _put_inode(image, 13, _inode(0x81A4, pointers=[950, 951, 952]))
    _mark_blocks(image, [60, 100, 700, 701, 800, 900, 901, 950, 951, 952])
    path.write_bytes(image)


def verify(path: Path, filesystem: str, files: int, directories: int,
           fragmented_files: int, fragmented_directories: int) -> None:
    result = BACKEND.map(str(path), 128)
    assert result["filesystem"] == filesystem, result
    assert result["regular_files"] == files, result
    assert result["directories"] == directories, result
    assert result["fragmented_files"] == fragmented_files, result
    assert result["fragmented_directories"] == fragmented_directories, result
    assert result["details"]["fragmentation_available"] is True, result
    assert result["details"]["malformed_inodes"] == 0, result
    assert sum(cell["fragmented"] for cell in result["cells"]) > 0, result
    assert sum(cell["directory"] for cell in result["cells"]) > 0, result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="linux-defragger-ext-") as directory:
        root = Path(directory)
        ext4 = root / "ext4.img"
        ext2 = root / "ext2.img"
        make_ext4(ext4)
        make_ext2(ext2)
        verify(ext4, "ext4", 3, 2, 1, 1)
        verify(ext2, "ext2", 2, 1, 1, 0)
    print("ext2/ext4 allocation and inode fragmentation tests passed")


if __name__ == "__main__":
    main()
