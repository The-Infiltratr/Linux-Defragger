#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: HFS+/HFSX analysis, compaction, defragmentation and recovery.

"""Native HFS+ and HFSX analysis backend.

The backend reads the volume header, allocation bitmap, catalog B-tree and
extents-overflow B-tree directly.  It never mounts or modifies the volume.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .base import (
    BackendError, BackendInfo, CAP_ANALYSE, CAP_MAP, CAP_COMPACT, CAP_DEFRAG, CAP_RECOVER, CAP_LIVE_MAP, Reader, aggregate_bitmap,
    u16be, u32be, u64be,
)

INFO = BackendInfo("hfsplus", "Apple HFS+/HFSX", ("hfsplus", "hfs+", "hfsx"),
                   CAP_ANALYSE | CAP_MAP | CAP_COMPACT | CAP_DEFRAG | CAP_RECOVER | CAP_LIVE_MAP, "exact")


@dataclass(frozen=True)
class Extent:
    start: int
    count: int


class ForkReader:
    """Read a fork through its physical allocation-block extents."""

    def __init__(self, reader: Reader, block_size: int, extents: list[Extent], logical_size: int):
        self.reader = reader
        self.block_size = block_size
        self.extents = [e for e in extents if e.count]
        self.logical_size = logical_size
        self._segments: list[tuple[int, int, int]] = []
        logical = 0
        for extent in self.extents:
            length = extent.count * block_size
            self._segments.append((logical, logical + length, extent.start * block_size))
            logical += length

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.logical_size:
            raise BackendError("HFS+ fork read is outside the logical file")
        out = bytearray()
        pos = offset
        remaining = length
        for lo, hi, physical in self._segments:
            if pos >= hi or pos < lo and pos + remaining <= lo:
                continue
            if pos < lo:
                raise BackendError("HFS+ fork contains an unrepresented extent")
            take = min(remaining, hi - pos)
            out.extend(self.reader.read(physical + (pos - lo), take))
            pos += take
            remaining -= take
            if not remaining:
                return bytes(out)
        raise BackendError("HFS+ fork extents do not cover the requested data")


def _fork(data: bytes, offset: int) -> tuple[int, int, list[Extent]]:
    logical_size = u64be(data, offset)
    total_blocks = u32be(data, offset + 12)
    extents = [Extent(u32be(data, offset + 16 + i * 8), u32be(data, offset + 20 + i * 8)) for i in range(8)]
    return logical_size, total_blocks, extents


def _record_offsets(node: bytes, count: int) -> list[int]:
    """Return record starts from the B-tree node's reverse offset table."""
    values = [u16be(node, len(node) - 2 * (i + 1)) for i in range(count + 1)]
    starts = sorted(set(values[:-1]))
    return [v for v in starts if 14 <= v < len(node)]


def _walk_leaf_records(fork: ForkReader):
    header_prefix = fork.read(0, min(fork.logical_size, 512))
    if len(header_prefix) < 40:
        raise BackendError("HFS+ B-tree header is truncated")
    node_size = u16be(header_prefix, 32)
    first_leaf = u32be(header_prefix, 24)
    total_nodes = u32be(header_prefix, 36)
    if node_size < 512 or node_size > 65536 or node_size & (node_size - 1):
        raise BackendError("invalid HFS+ B-tree node size")
    node_number = first_leaf
    seen: set[int] = set()
    while node_number:
        if node_number in seen or node_number >= total_nodes:
            raise BackendError("HFS+ B-tree leaf chain is cyclic or out of range")
        seen.add(node_number)
        node = fork.read(node_number * node_size, node_size)
        next_node = u32be(node, 0)
        kind = struct.unpack_from(">b", node, 8)[0]
        count = u16be(node, 10)
        if kind != -1:
            raise BackendError("HFS+ leaf chain points to a non-leaf node")
        starts = _record_offsets(node, count)
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else min(
                [u16be(node, len(node) - 2 * (i + 1)) for i in range(count + 1) if u16be(node, len(node) - 2 * (i + 1)) > start],
                default=len(node) - 2 * (count + 1),
            )
            if end > start:
                yield node[start:end]
        node_number = next_node


def _parse_extent_record(record: bytes) -> tuple[tuple[int, int, int], list[Extent]] | None:
    if len(record) < 12:
        return None
    key_len = u16be(record, 0)
    data_off = 2 + key_len
    if data_off & 1:
        data_off += 1
    if key_len < 10 or data_off + 64 > len(record):
        return None
    fork_type = record[2]
    file_id = u32be(record, 4)
    start_block = u32be(record, 8)
    extents = [Extent(u32be(record, data_off + i * 8), u32be(record, data_off + i * 8 + 4)) for i in range(8)]
    return (file_id, fork_type, start_block), [e for e in extents if e.count]


def _overflow_map(reader: Reader, block_size: int, fork_tuple) -> dict[tuple[int, int], list[Extent]]:
    logical_size, total_blocks, extents = fork_tuple
    if total_blocks > sum(e.count for e in extents):
        raise BackendError("HFS+ extents-overflow file itself uses overflow extents")
    if not logical_size:
        return {}
    fork = ForkReader(reader, block_size, extents, logical_size)
    grouped: dict[tuple[int, int], list[tuple[int, list[Extent]]]] = {}
    for raw in _walk_leaf_records(fork):
        parsed = _parse_extent_record(raw)
        if parsed:
            (file_id, fork_type, start_block), values = parsed
            grouped.setdefault((file_id, fork_type), []).append((start_block, values))
    result: dict[tuple[int, int], list[Extent]] = {}
    for key, parts in grouped.items():
        assembled: list[Extent] = []
        for _start, values in sorted(parts):
            assembled.extend(values)
        result[key] = assembled
    return result


def _fork_extents(record: bytes, offset: int, file_id: int, fork_type: int,
                  overflow: dict[tuple[int, int], list[Extent]]) -> list[Extent]:
    logical, total_blocks, inline = _fork(record, offset)
    values = [e for e in inline if e.count]
    if sum(e.count for e in values) < total_blocks:
        values.extend(overflow.get((file_id, fork_type), []))
    if sum(e.count for e in values) < total_blocks and logical:
        raise BackendError(f"HFS+ file {file_id} has unresolved overflow extents")
    return values


def _catalog_summary(reader: Reader, block_size: int, catalog_tuple, overflow):
    logical_size, total_blocks, extents = catalog_tuple
    if total_blocks > sum(e.count for e in extents):
        raise BackendError("HFS+ catalog file itself uses overflow extents")
    fork = ForkReader(reader, block_size, extents, logical_size)
    files = directories = fragmented_files = fragmented_directories = 0
    fragment_blocks: set[int] = set()
    directory_blocks: set[int] = set()
    for raw in _walk_leaf_records(fork):
        if len(raw) < 4:
            continue
        key_len = u16be(raw, 0)
        data_off = 2 + key_len
        if data_off & 1:
            data_off += 1
        if data_off + 2 > len(raw):
            continue
        record_type = struct.unpack_from(">h", raw, data_off)[0]
        if record_type == 1:  # folder record
            directories += 1
        elif record_type == 2 and data_off + 248 <= len(raw):
            files += 1
            file_id = u32be(raw, data_off + 8)
            data_extents = _fork_extents(raw, data_off + 88, file_id, 0, overflow)
            resource_extents = _fork_extents(raw, data_off + 168, file_id, 0xFF, overflow)
            def fork_fragmented(values):
                values = [e for e in values if e.count]
                return any(left.start + left.count != right.start
                           for left, right in zip(values, values[1:]))
            data_fragmented = fork_fragmented(data_extents)
            resource_fragmented = fork_fragmented(resource_extents)
            if data_fragmented or resource_fragmented:
                fragmented_files += 1
                for extent in data_extents if data_fragmented else []:
                    fragment_blocks.update(range(extent.start, extent.start + extent.count))
                for extent in resource_extents if resource_fragmented else []:
                    fragment_blocks.update(range(extent.start, extent.start + extent.count))
    # The catalog fork itself contains directory metadata.
    for extent in extents:
        directory_blocks.update(range(extent.start, extent.start + extent.count))
    return files, directories, fragmented_files, fragmented_directories, fragment_blocks, directory_blocks


class HFSPlusBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            return reader.read(1024, 2) in (b"H+", b"HX")

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            header = reader.read(1024, 512)
            signature = header[:2]
            if signature not in (b"H+", b"HX"):
                raise BackendError("not an HFS+ or HFSX volume")
            block_size = u32be(header, 40)
            total_blocks = u32be(header, 44)
            free_blocks = u32be(header, 48)
            file_count = u32be(header, 32)
            folder_count = u32be(header, 36)
            if block_size < 512 or block_size & (block_size - 1) or not total_blocks:
                raise BackendError("invalid HFS+ allocation geometry")
            allocation = _fork(header, 112)
            extents_overflow = _fork(header, 192)
            catalog = _fork(header, 272)
            alloc_logical, alloc_blocks, alloc_extents = allocation
            if alloc_blocks > sum(e.count for e in alloc_extents):
                raise BackendError("HFS+ allocation file uses unsupported overflow extents")
            bitmap_reader = ForkReader(reader, block_size, alloc_extents, alloc_logical)
            bitmap = bytes(int(f"{value:08b}"[::-1], 2) for value in bitmap_reader.read(0, (total_blocks + 7) // 8))
            overflow = _overflow_map(reader, block_size, extents_overflow)
            try:
                summary = _catalog_summary(reader, block_size, catalog, overflow)
                files, directories, frag_files, frag_dirs, frag_blocks, dir_blocks = summary
                catalog_complete = True
            except BackendError:
                # Allocation mapping remains exact even if an exotic catalog layout
                # prevents file-level statistics.
                files, directories = file_count, folder_count
                frag_files = frag_dirs = 0
                frag_blocks = set()
                dir_blocks = set()
                catalog_complete = False
            result = aggregate_bitmap(
                bitmap, total_blocks, cells, block_size,
                "hfsx" if signature == b"HX" else "hfsplus",
                details={"volume_version": u16be(header, 2), "journaled": bool(u32be(header, 4) & 0x2000)},
            )
            if catalog_complete:
                result.update({
                    "regular_files": files,
                    "directories": directories + 1,
                    "fragmented_files": frag_files,
                    "fragmented_directories": frag_dirs,
                })
            else:
                result["details"].update({"file_count": files, "folder_count": directories})
            for cell in result["cells"]:
                start, end = cell["start"], cell["end"]
                cell["fragmented"] = sum(1 for b in frag_blocks if start <= b <= end)
                cell["directory"] = sum(1 for b in dir_blocks if start <= b <= end)
            # Header free count is a useful independent consistency signal.
            result["details"]["header_free_blocks"] = free_blocks
            return result


BACKEND = HFSPlusBackend()
