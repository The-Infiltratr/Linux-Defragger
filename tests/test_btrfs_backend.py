#!/usr/bin/python3
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
import backends.btrfs as btrfs
from backends.btrfs import BtrfsBackend

NODE = 4096
SECTOR = 4096
TOTAL = 64 * 1024 * 1024
META_LOGICAL = 1 * 1024 * 1024
META_PHYS1 = 1 * 1024 * 1024
META_PHYS2 = 17 * 1024 * 1024
META_LENGTH = 16 * 1024 * 1024
DATA_LOGICAL = 32 * 1024 * 1024
DATA_PHYS = 40 * 1024 * 1024
DATA_LENGTH = 16 * 1024 * 1024


def key(objectid: int, typ: int, offset: int) -> bytes:
    return struct.pack("<QBQ", objectid, typ, offset)


def chunk_item(length: int, owner: int, typ: int, stripes: list[tuple[int, int]]) -> bytes:
    out = bytearray(48 + len(stripes) * 32)
    struct.pack_into("<QQQQIIIHH", out, 0, length, owner, 64 * 1024, typ,
                     SECTOR, SECTOR, SECTOR, len(stripes), 0)
    for i, (devid, physical) in enumerate(stripes):
        struct.pack_into("<QQ", out, 48 + i * 32, devid, physical)
    return bytes(out)


def root_item(bytenr: int, level: int = 0, refs: int = 1) -> bytes:
    out = bytearray(239)
    struct.pack_into("<Q", out, 160, 1)
    struct.pack_into("<Q", out, 176, bytenr)
    struct.pack_into("<I", out, 216, refs)
    out[238] = level
    return bytes(out)


def inode_item(mode: int, size: int) -> bytes:
    out = bytearray(160)
    struct.pack_into("<Q", out, 16, size)
    struct.pack_into("<I", out, 52, mode)
    return bytes(out)


def file_extent(disk_bytenr: int, length: int) -> bytes:
    out = bytearray(53)
    struct.pack_into("<QQ", out, 0, 1, length)
    out[20] = 1
    struct.pack_into("<QQQQ", out, 21, disk_bytenr, length, 0, length)
    return bytes(out)


def leaf(bytenr: int, owner: int, items: list[tuple[bytes, bytes]]) -> bytes:
    block = bytearray(NODE)
    struct.pack_into("<Q", block, 48, bytenr)
    struct.pack_into("<Q", block, 80, 1)
    struct.pack_into("<Q", block, 88, owner)
    struct.pack_into("<I", block, 96, len(items))
    block[100] = 0
    cursor = NODE
    for index, (raw_key, payload) in enumerate(items):
        cursor -= len(payload)
        block[cursor:cursor + len(payload)] = payload
        pos = 101 + index * 25
        block[pos:pos + 17] = raw_key
        struct.pack_into("<II", block, pos + 17, cursor - 101, len(payload))
    return bytes(block)


def node(bytenr: int, owner: int, children: list[tuple[bytes, int]]) -> bytes:
    block = bytearray(NODE)
    struct.pack_into("<Q", block, 48, bytenr)
    struct.pack_into("<Q", block, 80, 1)
    struct.pack_into("<Q", block, 88, owner)
    struct.pack_into("<I", block, 96, len(children))
    block[100] = 1
    for index, (raw_key, child) in enumerate(children):
        pos = 101 + index * 33
        block[pos:pos + 17] = raw_key
        struct.pack_into("<QQ", block, pos + 17, child, 1)
    return bytes(block)


def make_image(path: Path) -> None:
    image = bytearray(TOTAL)
    chunk_root = META_LOGICAL
    root_tree = META_LOGICAL + NODE
    extent_tree = META_LOGICAL + 2 * NODE
    fs_tree = META_LOGICAL + 3 * NODE
    fs_leaf_a = META_LOGICAL + 4 * NODE
    fs_leaf_b = META_LOGICAL + 5 * NODE
    metadata_chunk = chunk_item(META_LENGTH, 3, 2 | 4 | 32,
                                [(1, META_PHYS1), (1, META_PHYS2)])
    data_chunk = chunk_item(DATA_LENGTH, 3, 1, [(1, DATA_PHYS)])

    image[META_PHYS1:META_PHYS1 + NODE] = leaf(chunk_root, 3, [
        (key(256, 228, META_LOGICAL), metadata_chunk),
        (key(256, 228, DATA_LOGICAL), data_chunk),
    ])
    image[META_PHYS2:META_PHYS2 + NODE] = image[META_PHYS1:META_PHYS1 + NODE]

    root_leaf = leaf(root_tree, 1, [
        (key(2, 132, 1), root_item(extent_tree)),
        (key(5, 132, 1), root_item(fs_tree, level=1)),
    ])
    image[META_PHYS1 + NODE:META_PHYS1 + 2 * NODE] = root_leaf
    image[META_PHYS2 + NODE:META_PHYS2 + 2 * NODE] = root_leaf

    extent_items = []
    for logical in (chunk_root, root_tree, extent_tree, fs_tree, fs_leaf_a, fs_leaf_b):
        extent_items.append((key(logical, 169, 0), b""))
    extent_items.extend([
        (key(DATA_LOGICAL, 168, NODE), bytes(24)),
        (key(DATA_LOGICAL + 2 * NODE, 168, NODE), bytes(24)),
        (key(DATA_LOGICAL + 1024 * 1024, 168, 2 * NODE), bytes(24)),
    ])
    extent_leaf = leaf(extent_tree, 2, extent_items)
    image[META_PHYS1 + 2 * NODE:META_PHYS1 + 3 * NODE] = extent_leaf
    image[META_PHYS2 + 2 * NODE:META_PHYS2 + 3 * NODE] = extent_leaf

    first_leaf = leaf(fs_leaf_a, 5, [
        (key(256, 1, 0), inode_item(0o040755, NODE)),
        (key(257, 1, 0), inode_item(0o100644, 2 * NODE)),
        (key(257, 108, 0), file_extent(DATA_LOGICAL, NODE)),
        (key(257, 108, NODE), file_extent(DATA_LOGICAL + 2 * NODE, NODE)),
    ])
    second_leaf = leaf(fs_leaf_b, 5, [
        (key(258, 1, 0), inode_item(0o100644, 2 * NODE)),
        (key(258, 108, 0), file_extent(DATA_LOGICAL + 1024 * 1024, 2 * NODE)),
    ])
    fs_node = node(fs_tree, 5, [
        (key(256, 1, 0), fs_leaf_a),
        (key(258, 1, 0), fs_leaf_b),
    ])
    for base in (META_PHYS1, META_PHYS2):
        image[base + 3 * NODE:base + 4 * NODE] = fs_node
        image[base + 4 * NODE:base + 5 * NODE] = first_leaf
        image[base + 5 * NODE:base + 6 * NODE] = second_leaf


    sb = bytearray(4096)
    sb[0x40:0x48] = b"_BHRfS_M"
    struct.pack_into("<Q", sb, 48, 64 * 1024)
    struct.pack_into("<Q", sb, 72, 1)
    struct.pack_into("<Q", sb, 80, root_tree)
    struct.pack_into("<Q", sb, 88, chunk_root)
    struct.pack_into("<Q", sb, 112, TOTAL)
    struct.pack_into("<Q", sb, 120, 9 * NODE)
    struct.pack_into("<Q", sb, 136, 1)
    struct.pack_into("<II", sb, 144, SECTOR, NODE)
    struct.pack_into("<Q", sb, 201, 1)
    system = key(256, 228, META_LOGICAL) + metadata_chunk
    struct.pack_into("<I", sb, 160, len(system))
    sb[198] = 0
    sb[199] = 0
    sb[811:811 + len(system)] = system
    image[64 * 1024:64 * 1024 + 4096] = sb
    path.write_bytes(image)


def test_kernel_tree_search_parser() -> None:
    calls = 0
    payload = chunk_item(DATA_LENGTH, 3, 1, [(1, DATA_PHYS)])
    original = btrfs.fcntl.ioctl

    def fake_ioctl(fd, request_code, request, mutate=True):
        nonlocal calls
        assert fd == 9
        assert request_code == btrfs.BTRFS_IOC_TREE_SEARCH_V2
        assert struct.unpack_from("=I", request, 64)[0] == 131072
        if calls == 0:
            struct.pack_into("=I", request, 64, 1)
            struct.pack_into("=QQQII", request, 112, 7, 256, DATA_LOGICAL, 228, len(payload))
            request[144:144 + len(payload)] = payload
        else:
            struct.pack_into("=I", request, 64, 0)
        calls += 1
        return 0

    try:
        btrfs.fcntl.ioctl = fake_ioctl
        search = btrfs._KernelTreeSearch(9, 64 * 1024)
        items = list(search.items(3, 228))
    finally:
        btrfs.fcntl.ioctl = original
    assert len(items) == 1
    assert items[0].key == btrfs._Key(256, 228, DATA_LOGICAL)
    parsed = btrfs._parse_chunk(items[0].data, items[0].key.offset)
    assert parsed.length == DATA_LENGTH
    assert search.calls == 2



def test_kernel_tree_search_filters_intermediate_item_types_and_advances_full_key() -> None:
    calls = 0
    wanted_payload = chunk_item(DATA_LENGTH, 3, 1, [(1, DATA_PHYS)])
    other_payload = b'other'
    original = btrfs.fcntl.ioctl

    def fake_ioctl(fd, request_code, request, mutate=True):
        nonlocal calls
        assert request_code == btrfs.BTRFS_IOC_TREE_SEARCH_V2
        assert struct.unpack_from("=I", request, 64)[0] == 131072
        if calls == 0:
            struct.pack_into('=I', request, 64, 2)
            pos = 112
            struct.pack_into('=QQQII', request, pos, 1, 100, 5, 12, len(other_payload))
            pos += 32
            request[pos:pos + len(other_payload)] = other_payload
            pos += len(other_payload)
            struct.pack_into('=QQQII', request, pos, 1, 256, DATA_LOGICAL, 228, len(wanted_payload))
            pos += 32
            request[pos:pos + len(wanted_payload)] = wanted_payload
        else:
            struct.pack_into('=I', request, 64, 0)
        calls += 1
        return 0

    try:
        btrfs.fcntl.ioctl = fake_ioctl
        search = btrfs._KernelTreeSearch(9, 64 * 1024)
        items = list(search.items(3, 228))
    finally:
        btrfs.fcntl.ioctl = original
    assert [item.key.type for item in items] == [228]
    assert items[0].key.objectid == 256
    assert search.calls == 2
    assert search.filtered_items == 1

def main() -> int:
    test_kernel_tree_search_parser()
    test_kernel_tree_search_filters_intermediate_item_types_and_advances_full_key()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "btrfs.img"
        make_image(path)
        result = BtrfsBackend().map(str(path), 2048)
        assert result["map_accuracy"] == "exact-single-device"
        assert result["unknown_bytes"] == 0
        assert result["regular_files"] == 2
        assert result["directories"] == 1
        assert result["fragmented_files"] == 1
        assert result["fragmented_directories"] == 0
        assert result["free_bytes"] > 0
        assert sum(cell["fragmented"] for cell in result["cells"]) > 0
        print(json.dumps({k: result[k] for k in (
            "used_bytes", "free_bytes", "regular_files", "fragmented_files"
        )}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
