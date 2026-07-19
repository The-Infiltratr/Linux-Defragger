# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Read-only ext2, ext3 and ext4 allocation and fragmentation backend."""

from __future__ import annotations

import struct

from .base import *

INFO = BackendInfo("ext4", "ext2/3/4", ("ext2", "ext3", "ext4"), CAP_ANALYSE | CAP_MAP | CAP_COMPACT, "exact")

_EXT4_EXTENTS_FL = 0x00080000
_EXT4_INLINE_DATA_FL = 0x10000000
_EXT4_EXT_MAGIC = 0xF30A
_EXT4_FEATURE_COMPAT_HAS_JOURNAL = 0x0004
_EXT4_FEATURE_INCOMPAT_META_BG = 0x0010
_EXT4_FEATURE_INCOMPAT_EXTENTS = 0x0040
_EXT4_FEATURE_INCOMPAT_64BIT = 0x0080
_EXT4_FEATURE_RO_COMPAT_BIGALLOC = 0x0200
_EXT4_BG_INODE_UNINIT = 0x0001
_EXT4_S_IFMT = 0xF000
_EXT4_S_IFDIR = 0x4000
_EXT4_S_IFREG = 0x8000
_INODE_READ_CHUNK = 256 * 1024
_MAX_EXTENT_DEPTH = 5
_MAX_INDIRECT_DEPTH = 3


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if start < 0 or end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlay_ranges(cells: list[dict], ranges: list[tuple[int, int]], field: str) -> int:
    """Overlay merged filesystem-block ranges onto the allocation-map cells."""
    merged = _merge_ranges(ranges)
    range_index = 0
    total = sum(end - start for start, end in merged)
    for cell in cells:
        start = int(cell["start"])
        end_ex = int(cell["end"]) + 1
        while range_index < len(merged) and merged[range_index][1] <= start:
            range_index += 1
        overlap = 0
        check = range_index
        while check < len(merged) and merged[check][0] < end_ex:
            overlap += max(0, min(end_ex, merged[check][1]) - max(start, merged[check][0]))
            if merged[check][1] > end_ex:
                break
            check += 1
        cell[field] = min(int(cell.get("used", 0)), overlap)
    return total


def _extent_length(raw: int) -> int:
    # Values above 32768 mark unwritten extents; 32768 itself is a valid
    # initialized extent of that exact length.
    return raw - 0x8000 if raw > 0x8000 else raw


def _parse_extent_node(reader: Reader, data: bytes, block_size: int, total_blocks: int,
                       expected_depth: int | None, visited: set[int]) -> list[tuple[int, int, int]]:
    if len(data) < 12 or u16le(data, 0) != _EXT4_EXT_MAGIC:
        raise BackendError("invalid ext extent-tree header")
    entries = u16le(data, 2)
    maximum = u16le(data, 4)
    depth = u16le(data, 6)
    if expected_depth is not None and depth != expected_depth:
        raise BackendError("inconsistent ext extent-tree depth")
    if depth > _MAX_EXTENT_DEPTH or entries > maximum:
        raise BackendError("invalid ext extent-tree geometry")
    if 12 + entries * 12 > len(data):
        raise BackendError("truncated ext extent-tree node")

    extents: list[tuple[int, int, int]] = []
    previous_logical = -1
    if depth == 0:
        for index in range(entries):
            pos = 12 + index * 12
            logical = u32le(data, pos)
            length = _extent_length(u16le(data, pos + 4))
            physical = u32le(data, pos + 8) | (u16le(data, pos + 6) << 32)
            if length <= 0 or logical <= previous_logical:
                raise BackendError("invalid ext leaf extent")
            if physical <= 0 or physical + length > total_blocks:
                raise BackendError("ext extent points outside the filesystem")
            extents.append((logical, physical, length))
            previous_logical = logical
        return extents

    for index in range(entries):
        pos = 12 + index * 12
        logical = u32le(data, pos)
        child = u32le(data, pos + 4) | (u16le(data, pos + 8) << 32)
        if logical <= previous_logical or child <= 0 or child >= total_blocks:
            raise BackendError("invalid ext extent index")
        if child in visited:
            raise BackendError("loop in ext extent tree")
        visited.add(child)
        child_data = reader.read(child * block_size, block_size)
        extents.extend(
            _parse_extent_node(reader, child_data, block_size, total_blocks, depth - 1, visited)
        )
        previous_logical = logical
    return extents


def _extent_inode_blocks(reader: Reader, inode: bytes, block_size: int,
                         total_blocks: int) -> list[tuple[int, int, int]]:
    return _parse_extent_node(
        reader,
        inode[40:100],
        block_size,
        total_blocks,
        None,
        set(),
    )


def _pointer_block(reader: Reader, block: int, block_size: int,
                   total_blocks: int, visited: set[int]) -> tuple[int, ...]:
    if block <= 0 or block >= total_blocks:
        raise BackendError("ext indirect block points outside the filesystem")
    if block in visited:
        raise BackendError("loop in ext indirect block tree")
    visited.add(block)
    data = reader.read(block * block_size, block_size)
    return struct.unpack("<" + "I" * (block_size // 4), data)


def _indirect_inode_blocks(reader: Reader, inode: bytes, block_size: int,
                           total_blocks: int) -> list[tuple[int, int, int]]:
    pointers = struct.unpack_from("<15I", inode, 40)
    per_block = block_size // 4
    extents: list[tuple[int, int, int]] = []
    visited: set[int] = set()

    def add_data(logical: int, physical: int) -> None:
        if physical == 0:
            return
        if physical >= total_blocks:
            raise BackendError("ext data block points outside the filesystem")
        extents.append((logical, physical, 1))

    for index, physical in enumerate(pointers[:12]):
        add_data(index, physical)

    if pointers[12]:
        for index, physical in enumerate(
            _pointer_block(reader, pointers[12], block_size, total_blocks, visited)
        ):
            add_data(12 + index, physical)

    double_base = 12 + per_block
    if pointers[13]:
        top = _pointer_block(reader, pointers[13], block_size, total_blocks, visited)
        for outer, child in enumerate(top):
            if not child:
                continue
            second = _pointer_block(reader, child, block_size, total_blocks, visited)
            logical_base = double_base + outer * per_block
            for inner, physical in enumerate(second):
                add_data(logical_base + inner, physical)

    triple_base = double_base + per_block * per_block
    if pointers[14]:
        top = _pointer_block(reader, pointers[14], block_size, total_blocks, visited)
        for first, middle_block in enumerate(top):
            if not middle_block:
                continue
            middle = _pointer_block(reader, middle_block, block_size, total_blocks, visited)
            for second_index, leaf_block in enumerate(middle):
                if not leaf_block:
                    continue
                leaf = _pointer_block(reader, leaf_block, block_size, total_blocks, visited)
                logical_base = triple_base + (first * per_block + second_index) * per_block
                for third, physical in enumerate(leaf):
                    add_data(logical_base + third, physical)

    return extents


def _coalesce_extents(extents: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Join physically and logically adjacent runs; logical holes break an extent."""
    result: list[tuple[int, int, int]] = []
    previous_logical_end = -1
    for logical, physical, length in sorted(extents):
        if length <= 0:
            continue
        if logical < previous_logical_end:
            raise BackendError("overlapping ext file extents")
        if result:
            old_logical, old_physical, old_length = result[-1]
            if old_logical + old_length == logical and old_physical + old_length == physical:
                result[-1] = (old_logical, old_physical, old_length + length)
            else:
                result.append((logical, physical, length))
        else:
            result.append((logical, physical, length))
        previous_logical_end = logical + length
    return result


def _inode_extents(reader: Reader, inode: bytes, block_size: int,
                   total_blocks: int) -> list[tuple[int, int, int]]:
    flags = u32le(inode, 32)
    if flags & _EXT4_INLINE_DATA_FL:
        return []
    if flags & _EXT4_EXTENTS_FL:
        return _coalesce_extents(_extent_inode_blocks(reader, inode, block_size, total_blocks))
    return _coalesce_extents(_indirect_inode_blocks(reader, inode, block_size, total_blocks))


def _filesystem_name(compat: int, incompat: int) -> str:
    if incompat & _EXT4_FEATURE_INCOMPAT_EXTENTS:
        return "ext4"
    if compat & _EXT4_FEATURE_COMPAT_HAS_JOURNAL:
        return "ext3"
    return "ext2"


def _scan_fragmentation(reader: Reader, descriptors: list[dict], block_size: int,
                        total_blocks: int, total_inodes: int, inodes_per_group: int,
                        inode_size: int) -> dict:
    regular_files = directories = fragmented_files = fragmented_directories = 0
    inodes_scanned = malformed_inodes = 0
    fragmented_ranges: list[tuple[int, int]] = []
    directory_ranges: list[tuple[int, int]] = []

    chunk_inodes = max(8, _INODE_READ_CHUNK // inode_size)
    chunk_inodes -= chunk_inodes % 8

    for group, descriptor in enumerate(descriptors):
        first_inode = group * inodes_per_group
        valid_inodes = min(inodes_per_group, max(0, total_inodes - first_inode))
        if valid_inodes <= 0 or descriptor["flags"] & _EXT4_BG_INODE_UNINIT:
            continue
        bitmap_block = descriptor["inode_bitmap"]
        inode_table = descriptor["inode_table"]
        if bitmap_block <= 0 or bitmap_block >= total_blocks or inode_table <= 0 or inode_table >= total_blocks:
            malformed_inodes += valid_inodes
            continue
        bitmap = reader.read(bitmap_block * block_size, block_size)
        if len(bitmap) * 8 < valid_inodes:
            raise BackendError("ext inode bitmap is shorter than its block group")

        for chunk_start in range(0, valid_inodes, chunk_inodes):
            count = min(chunk_inodes, valid_inodes - chunk_start)
            byte_start = chunk_start // 8
            byte_end = (chunk_start + count + 7) // 8
            if not any(bitmap[byte_start:byte_end]):
                continue
            table_offset = inode_table * block_size + chunk_start * inode_size
            raw = reader.read(table_offset, count * inode_size)
            for local in range(count):
                bit = chunk_start + local
                if not (bitmap[bit >> 3] & (1 << (bit & 7))):
                    continue
                inode = raw[local * inode_size:(local + 1) * inode_size]
                mode = u16le(inode, 0) & _EXT4_S_IFMT
                if mode not in (_EXT4_S_IFREG, _EXT4_S_IFDIR):
                    continue
                inodes_scanned += 1
                is_directory = mode == _EXT4_S_IFDIR
                if is_directory:
                    directories += 1
                else:
                    regular_files += 1
                try:
                    extents = _inode_extents(reader, inode, block_size, total_blocks)
                except (BackendError, struct.error):
                    malformed_inodes += 1
                    continue
                ranges = [(physical, physical + length) for _logical, physical, length in extents]
                fragmented = len(extents) > 1
                if is_directory:
                    directory_ranges.extend(ranges)
                    if fragmented:
                        fragmented_directories += 1
                        fragmented_ranges.extend(ranges)
                elif fragmented:
                    fragmented_files += 1
                    fragmented_ranges.extend(ranges)

    return {
        "regular_files": regular_files,
        "directories": directories,
        "fragmented_files": fragmented_files,
        "fragmented_directories": fragmented_directories,
        "fragmentation_percent": 100.0 * fragmented_files / max(1, regular_files),
        "inodes_scanned": inodes_scanned,
        "malformed_inodes": malformed_inodes,
        "fragmented_ranges": _merge_ranges(fragmented_ranges),
        "directory_ranges": _merge_ranges(directory_ranges),
    }


class ExtBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            superblock = reader.read(1024, 1024)
            return u16le(superblock, 56) == 0xEF53

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            superblock = reader.read(1024, 1024)
            if u16le(superblock, 56) != 0xEF53:
                raise BackendError("not an ext filesystem")

            total_inodes = u32le(superblock, 0)
            block_size = 1024 << u32le(superblock, 24)
            blocks_lo = u32le(superblock, 4)
            compat = u32le(superblock, 92)
            incompat = u32le(superblock, 96)
            ro_compat = u32le(superblock, 100)
            has_64bit = bool(incompat & _EXT4_FEATURE_INCOMPAT_64BIT)
            blocks_hi = u32le(superblock, 0x150) if has_64bit else 0
            total_blocks = blocks_lo | (blocks_hi << 32)
            first_data = u32le(superblock, 20)
            blocks_per_group = u32le(superblock, 32)
            inodes_per_group = u32le(superblock, 40)
            inode_size = u16le(superblock, 88) or 128
            desc_size = u16le(superblock, 0xFE) if has_64bit else 32
            desc_size = max(32, desc_size)
            filesystem = _filesystem_name(compat, incompat)

            if not total_blocks or not blocks_per_group or not total_inodes or not inodes_per_group:
                raise BackendError("invalid ext geometry")
            if block_size < 1024 or block_size > 65536 or block_size & (block_size - 1):
                raise BackendError("unsupported ext block size")
            if inode_size < 128 or inode_size > block_size or inode_size & 3:
                raise BackendError("unsupported ext inode size")
            groups = (total_blocks - first_data + blocks_per_group - 1) // blocks_per_group
            desc_table_block = 2 if block_size == 1024 else 1
            desc_table_off = desc_table_block * block_size
            descriptors: list[dict] = []
            block_bitmaps: list[bytes | None] = []
            group_lengths: list[int] = []
            for group in range(groups):
                descriptor = reader.read(desc_table_off + group * desc_size, desc_size)
                block_bitmap = u32le(descriptor, 0)
                inode_bitmap = u32le(descriptor, 4)
                inode_table = u32le(descriptor, 8)
                if has_64bit and desc_size >= 64:
                    block_bitmap |= u32le(descriptor, 32) << 32
                    inode_bitmap |= u32le(descriptor, 36) << 32
                    inode_table |= u32le(descriptor, 40) << 32
                flags = u16le(descriptor, 18)
                group_start = first_data + group * blocks_per_group
                group_count = min(blocks_per_group, total_blocks - group_start)
                group_lengths.append(group_count)
                descriptors.append({
                    "block_bitmap": block_bitmap,
                    "inode_bitmap": inode_bitmap,
                    "inode_table": inode_table,
                    "flags": flags,
                })
                block_bitmaps.append(
                    reader.read(block_bitmap * block_size, block_size)
                    if 0 < block_bitmap < total_blocks else None
                )

            cell_count = max(1, min(cells, total_blocks))
            out = []
            free_total = used_total = unknown_total = 0
            for cell_index in range(cell_count):
                start = (cell_index * total_blocks) // cell_count
                end_ex = ((cell_index + 1) * total_blocks) // cell_count
                free = used = unknown = 0
                position = start
                if position < first_data:
                    count = min(end_ex, first_data) - position
                    unknown += count
                    position += count
                while position < end_ex:
                    relative = position - first_data
                    group = relative // blocks_per_group
                    within = relative % blocks_per_group
                    take = min(end_ex - position, group_lengths[group] - within)
                    bitmap = block_bitmaps[group]
                    if bitmap is None:
                        unknown += take
                    else:
                        set_bits = count_set_bits(bitmap, within, within + take)
                        used += set_bits
                        free += take - set_bits
                    position += take
                free_total += free
                used_total += used
                unknown_total += unknown
                out.append({
                    "start": start,
                    "end": end_ex - 1,
                    "free": free,
                    "used": used,
                    "unknown": unknown,
                    "bad": 0,
                    "fragmented": 0,
                    "directory": 0,
                })

            result = {
                "schema": 1,
                "backend": "read-only-domain",
                "filesystem": filesystem,
                "map_accuracy": "exact",
                "unit_size": block_size,
                "total_units": total_blocks,
                "cell_count": cell_count,
                "total_bytes": total_blocks * block_size,
                "free_bytes": free_total * block_size,
                "used_bytes": used_total * block_size,
                "unknown_bytes": unknown_total * block_size,
                "cells": out,
                "details": {
                    "block_size": block_size,
                    "groups": groups,
                    "inode_size": inode_size,
                },
            }

            details = result["details"]
            if ro_compat & _EXT4_FEATURE_RO_COMPAT_BIGALLOC:
                details["fragmentation_available"] = False
                details["fragmentation_note"] = "ext bigalloc fragmentation scanning is not yet supported"
                return result
            if incompat & _EXT4_FEATURE_INCOMPAT_META_BG:
                details["fragmentation_available"] = False
                details["fragmentation_note"] = "ext meta_bg fragmentation scanning is not yet supported"
                return result
            try:
                summary = _scan_fragmentation(
                    reader,
                    descriptors,
                    block_size,
                    total_blocks,
                    total_inodes,
                    inodes_per_group,
                    inode_size,
                )
            except BackendError as exc:
                details["fragmentation_available"] = False
                details["fragmentation_note"] = str(exc)
                return result

            fragmented_blocks = _overlay_ranges(
                result["cells"], summary["fragmented_ranges"], "fragmented"
            )
            directory_blocks = _overlay_ranges(
                result["cells"], summary["directory_ranges"], "directory"
            )
            result.update({
                "regular_files": summary["regular_files"],
                "directories": summary["directories"],
                "fragmented_files": summary["fragmented_files"],
                "fragmented_directories": summary["fragmented_directories"],
                "fragmentation_percent": summary["fragmentation_percent"],
            })
            details.update({
                "fragmentation_available": True,
                "fragmentation_basis": "ext inode allocation and physical extent trees",
                "inodes_scanned": summary["inodes_scanned"],
                "malformed_inodes": summary["malformed_inodes"],
                "fragmented_blocks_mapped": fragmented_blocks,
                "directory_blocks_mapped": directory_blocks,
            })
            return result


BACKEND = ExtBackend()
