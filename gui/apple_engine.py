#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Native, journalled HFS+/HFSX relocation engine.

"""Offline HFS+/HFSX compaction, defragmentation and recovery.

The engine moves complete data or resource forks into contiguous allocation
runs.  It updates the allocation file, catalogue records, extents-overflow
records and both volume headers through an external transaction journal.  A
move is committed only after all extent descriptors are durable; recovery
rolls back a partial metadata switch or completes a committed switch.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import signal
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

STOP = False


def _sigint(_signum, _frame):
    global STOP
    STOP = True


signal.signal(signal.SIGINT, _sigint)


class Error(RuntimeError):
    pass


def be16(data: bytes, off: int) -> int:
    return struct.unpack_from(">H", data, off)[0]


def be32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def be64(data: bytes, off: int) -> int:
    return struct.unpack_from(">Q", data, off)[0]


def pack32(value: int) -> bytes:
    return struct.pack(">I", value)


def mounted(path: str) -> bool:
    real = os.path.realpath(path)
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip().split(" - ", 1)
                if len(parts) != 2:
                    continue
                tail = parts[1].split()
                source = tail[1] if len(tail) > 1 else ""
                if source and os.path.realpath(source) == real:
                    return True
    except OSError:
        pass
    return False


def journal_write(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


@dataclass(frozen=True)
class Extent:
    start: int
    count: int


@dataclass(frozen=True)
class Patch:
    offset: int
    old: bytes
    new: bytes


class ForkMap:
    """Map logical fork offsets onto physical byte ranges."""

    def __init__(self, volume: "Volume", logical_size: int, total_blocks: int,
                 extents: list[Extent]):
        self.volume = volume
        self.logical_size = logical_size
        self.total_blocks = total_blocks
        self.extents = [e for e in extents if e.count]
        self.segments: list[tuple[int, int, int]] = []
        logical = 0
        for extent in self.extents:
            length = extent.count * volume.block_size
            self.segments.append((logical, logical + length,
                                  extent.start * volume.block_size))
            logical += length
        if total_blocks > sum(e.count for e in self.extents):
            raise Error("special HFS+ fork uses unresolved overflow extents")

    def pieces(self, offset: int, length: int) -> list[tuple[int, int]]:
        if offset < 0 or length < 0 or offset + length > self.logical_size:
            raise Error("fork access is outside its logical size")
        out: list[tuple[int, int]] = []
        pos = offset
        left = length
        for lo, hi, physical in self.segments:
            if not left:
                break
            if pos >= hi:
                continue
            if pos < lo:
                raise Error("fork has an unrepresented logical hole")
            take = min(left, hi - pos)
            out.append((physical + (pos - lo), take))
            pos += take
            left -= take
        if left:
            raise Error("fork extents do not cover requested bytes")
        return out

    def read(self, offset: int, length: int) -> bytes:
        out = bytearray()
        for physical, count in self.pieces(offset, length):
            out.extend(self.volume.read(physical, count))
        return bytes(out)

    def write(self, offset: int, data: bytes) -> None:
        pos = 0
        for physical, count in self.pieces(offset, len(data)):
            self.volume.write(physical, data[pos:pos + count])
            pos += count

    def patches(self, offset: int, new_data: bytes) -> list[Patch]:
        patches: list[Patch] = []
        pos = 0
        for physical, count in self.pieces(offset, len(new_data)):
            old = self.volume.read(physical, count)
            new = new_data[pos:pos + count]
            if old != new:
                patches.append(Patch(physical, old, new))
            pos += count
        return patches


def parse_fork(data: bytes, offset: int) -> tuple[int, int, list[Extent]]:
    logical_size = be64(data, offset)
    total_blocks = be32(data, offset + 12)
    extents = [Extent(be32(data, offset + 16 + i * 8),
                      be32(data, offset + 20 + i * 8)) for i in range(8)]
    return logical_size, total_blocks, extents


def record_offsets(node: bytes, count: int) -> list[int]:
    values = [be16(node, len(node) - 2 * (i + 1)) for i in range(count + 1)]
    starts = sorted(set(values[:-1]))
    return [value for value in starts if 14 <= value < len(node)]


@dataclass
class TreeRecord:
    fork: ForkMap
    logical_offset: int
    raw: bytes

    def patch(self, relative: int, data: bytes) -> list[Patch]:
        return self.fork.patches(self.logical_offset + relative, data)


def walk_leaf_records(fork: ForkMap) -> Iterable[TreeRecord]:
    prefix = fork.read(0, min(fork.logical_size, 512))
    if len(prefix) < 40:
        raise Error("HFS+ B-tree header is truncated")
    node_size = be16(prefix, 32)
    first_leaf = be32(prefix, 24)
    total_nodes = be32(prefix, 36)
    if node_size < 512 or node_size > 65536 or node_size & (node_size - 1):
        raise Error("invalid HFS+ B-tree node size")
    node_number = first_leaf
    seen: set[int] = set()
    while node_number:
        if node_number in seen or node_number >= total_nodes:
            raise Error("HFS+ B-tree leaf chain is invalid")
        seen.add(node_number)
        node_logical = node_number * node_size
        node = fork.read(node_logical, node_size)
        next_node = be32(node, 0)
        kind = struct.unpack_from(">b", node, 8)[0]
        count = be16(node, 10)
        if kind != -1:
            raise Error("HFS+ B-tree leaf chain points to a non-leaf node")
        starts = record_offsets(node, count)
        table_values = [be16(node, len(node) - 2 * (i + 1)) for i in range(count + 1)]
        for index, start in enumerate(starts):
            greater = [value for value in table_values if value > start]
            end = starts[index + 1] if index + 1 < len(starts) else min(
                greater, default=len(node) - 2 * (count + 1))
            if end > start:
                yield TreeRecord(fork, node_logical + start, node[start:end])
        node_number = next_node


@dataclass
class DescriptorRef:
    record: TreeRecord
    relative: int
    extent: Extent


@dataclass
class ForkObject:
    path: str
    file_id: int
    fork_type: int
    logical_size: int
    total_blocks: int
    descriptors: list[DescriptorRef]

    @property
    def extents(self) -> list[Extent]:
        return [item.extent for item in self.descriptors if item.extent.count]

    @property
    def fragments(self) -> int:
        values = self.extents
        if not values:
            return 0
        return 1 + sum(1 for left, right in zip(values, values[1:])
                       if left.start + left.count != right.start)

    @property
    def source_blocks(self) -> list[int]:
        out: list[int] = []
        for extent in self.extents:
            out.extend(range(extent.start, extent.start + extent.count))
        return out

    @property
    def label(self) -> str:
        return "resource" if self.fork_type == 0xFF else "data"


class Volume:
    def __init__(self, path: str, writable: bool = False, allow_inconsistent: bool = False):
        self.path = path
        flags = os.O_RDWR if writable else os.O_RDONLY
        self.fd = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
        self.writable = writable
        self.size = os.lseek(self.fd, 0, os.SEEK_END)
        os.lseek(self.fd, 0, os.SEEK_SET)
        self.primary_offset = 1024
        self.alternate_offset = self.size - 1024
        self.header = bytearray(self.read(self.primary_offset, 512))
        if self.header[:2] not in (b"H+", b"HX"):
            self.close()
            raise Error("not an HFS+ or HFSX volume")
        self.signature = self.header[:2].decode("ascii")
        self.version = be16(self.header, 2)
        self.attributes = be32(self.header, 4)
        self.block_size = be32(self.header, 40)
        self.total_blocks = be32(self.header, 44)
        self.free_blocks = be32(self.header, 48)
        self.create_date = be32(self.header, 16)
        self.write_count = be32(self.header, 68)
        if self.block_size < 512 or self.block_size & (self.block_size - 1):
            self.close()
            raise Error("invalid HFS+ allocation block size")
        if not self.total_blocks or self.total_blocks * self.block_size > self.size:
            self.close()
            raise Error("HFS+ geometry exceeds the device")
        if self.attributes & 0x00000800 and not allow_inconsistent:
            self.close()
            raise Error("HFS+ volume is marked inconsistent")
        allocation = parse_fork(self.header, 112)
        extents_fork = parse_fork(self.header, 192)
        catalog_fork = parse_fork(self.header, 272)
        self.allocation_fork = ForkMap(self, *allocation)
        self.extents_fork = ForkMap(self, *extents_fork)
        self.catalog_fork = ForkMap(self, *catalog_fork)
        bitmap_len = (self.total_blocks + 7) // 8
        if self.allocation_fork.logical_size < bitmap_len:
            self.close()
            raise Error("HFS+ allocation file is too short")
        self.bitmap = bytearray(self.allocation_fork.read(0, bitmap_len))
        self._overflow = self._parse_overflow()

    def close(self) -> None:
        if getattr(self, "fd", -1) >= 0:
            os.close(self.fd)
            self.fd = -1

    def read(self, offset: int, count: int) -> bytes:
        data = os.pread(self.fd, count, offset)
        if len(data) != count:
            raise Error(f"short read at byte {offset}")
        return data

    def write(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise Error("volume is open read-only")
        written = os.pwrite(self.fd, data, offset)
        if written != len(data):
            raise Error(f"short write at byte {offset}")

    def sync(self) -> None:
        os.fsync(self.fd)

    def bit(self, block: int) -> int:
        if block < 0 or block >= self.total_blocks:
            raise Error(f"invalid HFS+ allocation block {block}")
        return 1 if self.bitmap[block >> 3] & (0x80 >> (block & 7)) else 0

    def set_bit(self, block: int, value: bool) -> None:
        mask = 0x80 >> (block & 7)
        if value:
            self.bitmap[block >> 3] |= mask
        else:
            self.bitmap[block >> 3] &= ~mask

    def _parse_overflow(self) -> dict[tuple[int, int], list[DescriptorRef]]:
        grouped: dict[tuple[int, int], list[tuple[int, list[DescriptorRef]]]] = {}
        if not self.extents_fork.logical_size:
            return {}
        for record in walk_leaf_records(self.extents_fork):
            raw = record.raw
            if len(raw) < 12:
                continue
            key_len = be16(raw, 0)
            data_off = 2 + key_len
            if data_off & 1:
                data_off += 1
            if key_len < 10 or data_off + 64 > len(raw):
                continue
            fork_type = raw[2]
            file_id = be32(raw, 4)
            start_block = be32(raw, 8)
            refs: list[DescriptorRef] = []
            for index in range(8):
                relative = data_off + index * 8
                extent = Extent(be32(raw, relative), be32(raw, relative + 4))
                if extent.count:
                    refs.append(DescriptorRef(record, relative, extent))
            grouped.setdefault((file_id, fork_type), []).append((start_block, refs))
        result: dict[tuple[int, int], list[DescriptorRef]] = {}
        for key, parts in grouped.items():
            refs: list[DescriptorRef] = []
            for _start, values in sorted(parts):
                refs.extend(values)
            result[key] = refs
        return result

    def _inline_refs(self, record: TreeRecord, fork_offset: int) -> tuple[int, int, list[DescriptorRef]]:
        raw = record.raw
        logical_size = be64(raw, fork_offset)
        total_blocks = be32(raw, fork_offset + 12)
        refs: list[DescriptorRef] = []
        for index in range(8):
            relative = fork_offset + 16 + index * 8
            extent = Extent(be32(raw, relative), be32(raw, relative + 4))
            if extent.count:
                refs.append(DescriptorRef(record, relative, extent))
        return logical_size, total_blocks, refs

    def objects(self) -> list[ForkObject]:
        records: list[tuple[TreeRecord, int, int, str, int]] = []
        folder_names: dict[int, tuple[int, str]] = {2: (1, "")}
        for record in walk_leaf_records(self.catalog_fork):
            raw = record.raw
            if len(raw) < 4:
                continue
            key_len = be16(raw, 0)
            data_off = 2 + key_len
            if data_off & 1:
                data_off += 1
            if key_len < 6 or data_off + 2 > len(raw):
                continue
            parent_id = be32(raw, 2)
            name_len = be16(raw, 6)
            name_end = 8 + name_len * 2
            if name_end > len(raw):
                continue
            name = raw[8:name_end].decode("utf-16-be", "replace")
            record_type = struct.unpack_from(">h", raw, data_off)[0]
            if record_type == 1 and data_off + 12 <= len(raw):
                folder_id = be32(raw, data_off + 8)
                folder_names[folder_id] = (parent_id, name)
            elif record_type == 2 and data_off + 248 <= len(raw):
                file_id = be32(raw, data_off + 8)
                records.append((record, data_off, parent_id, name, file_id))

        def folder_path(folder_id: int) -> str:
            parts: list[str] = []
            seen: set[int] = set()
            current = folder_id
            while current not in (0, 1, 2) and current not in seen:
                seen.add(current)
                parent, name = folder_names.get(current, (2, f"#{current}"))
                if name:
                    parts.append(name)
                current = parent
            return "/".join(reversed(parts))

        out: list[ForkObject] = []
        for record, data_off, parent_id, name, file_id in records:
            base = folder_path(parent_id)
            path = f"{base}/{name}" if base else name
            for fork_type, fork_relative in ((0, data_off + 88), (0xFF, data_off + 168)):
                logical, total, refs = self._inline_refs(record, fork_relative)
                if sum(ref.extent.count for ref in refs) < total:
                    refs.extend(self._overflow.get((file_id, fork_type), []))
                if sum(ref.extent.count for ref in refs) < total and logical:
                    raise Error(f"HFS+ file {file_id} has unresolved overflow extents")
                if total:
                    out.append(ForkObject(path, file_id, fork_type, logical, total, refs))
        return out

    def free_run(self, count: int, low: bool = True, exclude: set[int] | None = None) -> int | None:
        exclude = exclude or set()
        order = range(self.total_blocks) if low else range(self.total_blocks - 1, -1, -1)
        run = 0
        start: int | None = None
        previous: int | None = None
        for block in order:
            adjacent = previous is None or (block == previous + 1 if low else block == previous - 1)
            if not self.bit(block) and block not in exclude and adjacent:
                if start is None:
                    start = block
                run += 1
                if run >= count:
                    return start if low else block
            else:
                run = 0
                start = None
            previous = block
        return None

    def bitmap_patches(self, before: bytes, after: bytes) -> list[Patch]:
        """Return physical patches for only the allocation-bitmap bytes changed."""
        patches: list[Patch] = []
        index = 0
        while index < len(before):
            if before[index] == after[index]:
                index += 1
                continue
            start = index
            while index < len(before) and before[index] != after[index]:
                index += 1
            old_data = before[start:index]
            new_data = after[start:index]
            pos = 0
            for physical, count in self.allocation_fork.pieces(start, len(new_data)):
                patches.append(Patch(physical, old_data[pos:pos + count],
                                     new_data[pos:pos + count]))
                pos += count
        return patches

    def header_patches(self, free_blocks: int) -> list[Patch]:
        new = pack32(free_blocks)
        return [Patch(self.primary_offset + 48, self.read(self.primary_offset + 48, 4), new),
                Patch(self.alternate_offset + 48, self.read(self.alternate_offset + 48, 4), new)]


def serialise_patches(patches: list[Patch]) -> list[dict]:
    return [{"offset": p.offset, "old": b64(p.old), "new": b64(p.new)} for p in patches]


def deserialise_patches(items: list[dict]) -> list[Patch]:
    return [Patch(int(item["offset"]), unb64(item["old"]), unb64(item["new"])) for item in items]


def apply_patches(volume: Volume, patches: list[Patch], use_new: bool) -> None:
    for patch in patches:
        volume.write(patch.offset, patch.new if use_new else patch.old)


def descriptor_patches(obj: ForkObject, destination_start: int) -> list[Patch]:
    patches: list[Patch] = []
    cursor = destination_start
    for ref in obj.descriptors:
        new = pack32(cursor) + pack32(ref.extent.count)
        patches.extend(ref.record.patch(ref.relative, new))
        cursor += ref.extent.count
    if cursor != destination_start + obj.total_blocks:
        raise Error("HFS+ extent descriptors do not match the fork block count")
    return patches


def read_blocks(volume: Volume, blocks: list[int]) -> bytes:
    out = bytearray(len(blocks) * volume.block_size)
    for index, block in enumerate(blocks):
        out[index * volume.block_size:(index + 1) * volume.block_size] = volume.read(
            block * volume.block_size, volume.block_size)
    return bytes(out)


def write_blocks(volume: Volume, blocks: list[int], data: bytes) -> None:
    if len(data) != len(blocks) * volume.block_size:
        raise Error("HFS+ relocation buffer size mismatch")
    for index, block in enumerate(blocks):
        volume.write(block * volume.block_size,
                     data[index * volume.block_size:(index + 1) * volume.block_size])


def make_bitmap_states(volume: Volume, src: list[int], dst: list[int]) -> tuple[bytes, bytes, bytes]:
    old = bytes(volume.bitmap)
    stage = bytearray(old)
    final = bytearray(old)
    for block in dst:
        stage[block >> 3] |= 0x80 >> (block & 7)
        final[block >> 3] |= 0x80 >> (block & 7)
    for block in src:
        final[block >> 3] &= ~(0x80 >> (block & 7))
    return old, bytes(stage), bytes(final)


def failpoint(stage: str) -> None:
    """Test-only interruption point used by disposable-image recovery tests."""
    if os.environ.get("LINUX_DEFRAGGER_TEST_FAIL_STAGE") == stage:
        raise Error(f"simulated interruption at {stage}")


def move_one(volume: Volume, obj: ForkObject, destination_start: int, journal: str) -> None:
    src = obj.source_blocks
    dst = list(range(destination_start, destination_start + obj.total_blocks))
    if any(volume.bit(block) for block in dst):
        raise Error("HFS+ destination is not free")
    old_bitmap, stage_bitmap, final_bitmap = make_bitmap_states(volume, src, dst)
    stage_bitmap_patches = volume.bitmap_patches(old_bitmap, stage_bitmap)
    final_bitmap_patches = volume.bitmap_patches(old_bitmap, final_bitmap)
    metadata_patches = descriptor_patches(obj, destination_start)
    stage_headers = volume.header_patches(volume.free_blocks - obj.total_blocks)
    final_headers = volume.header_patches(volume.free_blocks)
    transaction = {
        "schema": 1,
        "filesystem": "hfsplus",
        "device": volume.path,
        "signature": volume.signature,
        "create_date": volume.create_date,
        "block_size": volume.block_size,
        "total_blocks": volume.total_blocks,
        "path": obj.path,
        "file_id": obj.file_id,
        "fork_type": obj.fork_type,
        "src": src,
        "dst": dst,
        "stage": "prepared",
        "bitmap_stage": serialise_patches(stage_bitmap_patches),
        "bitmap_final": serialise_patches(final_bitmap_patches),
        "metadata": serialise_patches(metadata_patches),
        "headers_stage": serialise_patches(stage_headers),
        "headers_final": serialise_patches(final_headers),
    }
    journal_write(journal, transaction)
    payload = read_blocks(volume, src)
    write_blocks(volume, dst, payload)
    volume.sync()
    transaction["stage"] = "copied"
    journal_write(journal, transaction)
    failpoint("copied")

    apply_patches(volume, stage_bitmap_patches, True)
    apply_patches(volume, stage_headers, True)
    volume.sync()
    transaction["stage"] = "destination-ready"
    journal_write(journal, transaction)
    failpoint("destination-ready")

    transaction["stage"] = "switching"
    journal_write(journal, transaction)
    apply_patches(volume, metadata_patches, True)
    volume.sync()
    transaction["stage"] = "switched"
    journal_write(journal, transaction)
    failpoint("switched")

    apply_patches(volume, final_bitmap_patches, True)
    apply_patches(volume, final_headers, True)
    volume.sync()
    transaction["stage"] = "complete"
    journal_write(journal, transaction)
    os.unlink(journal)


def recover(device: str, journal: str) -> None:
    if mounted(device):
        raise Error("refusing to recover a mounted Apple volume")
    if not os.path.exists(journal):
        raise Error("no recovery journal")
    with open(journal, "r", encoding="utf-8") as fh:
        transaction = json.load(fh)
    if transaction.get("filesystem") != "hfsplus":
        raise Error("journal is not an HFS+ transaction")
    volume = Volume(device, True, allow_inconsistent=True)
    try:
        if (volume.signature != transaction.get("signature") or
                volume.create_date != int(transaction.get("create_date", -1)) or
                volume.block_size != int(transaction.get("block_size", -1)) or
                volume.total_blocks != int(transaction.get("total_blocks", -1))):
            raise Error("journal does not match this HFS+ volume")
        metadata = deserialise_patches(transaction["metadata"])
        stage_bitmap = deserialise_patches(transaction["bitmap_stage"])
        final_bitmap = deserialise_patches(transaction["bitmap_final"])
        stage_headers = deserialise_patches(transaction["headers_stage"])
        final_headers = deserialise_patches(transaction["headers_final"])
        current = [volume.read(p.offset, len(p.new)) for p in metadata]
        all_new = bool(metadata) and all(value == patch.new for value, patch in zip(current, metadata))
        all_old = all(value == patch.old for value, patch in zip(current, metadata))
        if all_new or transaction.get("stage") in {"switched", "complete"}:
            apply_patches(volume, metadata, True)
            apply_patches(volume, final_bitmap, True)
            apply_patches(volume, final_headers, True)
            result = "completed the committed destination"
        else:
            # Mixed metadata is always rolled back because every source block
            # remains allocated until after the metadata switch is durable.
            apply_patches(volume, metadata, False)
            apply_patches(volume, stage_bitmap, False)
            apply_patches(volume, stage_headers, False)
            result = "rolled back the uncommitted destination"
        volume.sync()
        os.unlink(journal)
        print(f"Recovery {result}.", flush=True)
    finally:
        volume.close()


def fragmentation(objects: list[ForkObject]) -> tuple[int, int]:
    files: dict[int, bool] = {}
    for obj in objects:
        files[obj.file_id] = files.get(obj.file_id, False) or obj.fragments > 1
    return sum(files.values()), 0


def emit_live_map(device: str, cells: int) -> None:
    try:
        # Imported lazily so the engine can also run from a source checkout.
        from backends.hfsplus import BACKEND
        result = BACKEND.map(device, cells)
        payload = {
            "fragmented_files": result.get("fragmented_files", 0),
            "fragmented_directories": result.get("fragmented_directories", 0),
            "free_clusters": result.get("free_bytes", 0) // max(1, result.get("unit_size", 1)),
            "cells": [{"i": index, **cell} for index, cell in enumerate(result.get("cells", []))],
        }
        print("@@LIVE_MAP " + json.dumps(payload, separators=(",", ":")), flush=True)
    except Exception:
        pass


def operation(device: str, mode: str, journal: str, max_files: int | None,
              live_cells: int | None) -> None:
    if mounted(device):
        raise Error("refusing to modify a mounted HFS+ volume")
    if os.path.exists(journal):
        raise Error("unfinished journal exists; run Recover first")
    moved = 0
    while not STOP:
        volume = Volume(device, True)
        try:
            objects = volume.objects()
            selected: ForkObject | None = None
            destination: int | None = None
            if mode == "defrag":
                candidates = sorted((obj for obj in objects if obj.fragments > 1),
                                    key=lambda obj: (-obj.fragments, -obj.total_blocks,
                                                     obj.path, obj.fork_type))
                for obj in candidates:
                    run = volume.free_run(obj.total_blocks, True, set(obj.source_blocks))
                    if run is not None:
                        selected, destination = obj, run
                        break
            else:
                # Pack complete forks into the lowest suitable free range.
                candidates = sorted(objects, key=lambda obj: (min(obj.source_blocks), obj.path,
                                                               obj.fork_type))
                for obj in candidates:
                    run = volume.free_run(obj.total_blocks, True, set(obj.source_blocks))
                    if run is not None and run < min(obj.source_blocks):
                        selected, destination = obj, run
                        break
            if selected is None or destination is None:
                break
            print(f"move: FILE {selected.path} [{selected.label} fork] "
                  f"({selected.total_blocks} blocks, {selected.fragments} fragments) "
                  f"-> block {destination}", flush=True)
            move_one(volume, selected, destination, journal)
            moved += 1
        finally:
            volume.close()
        if live_cells:
            emit_live_map(device, live_cells)
        if max_files and moved >= max_files:
            break
    print(f"Relocated {moved} HFS+/HFSX forks.", flush=True)
    if STOP:
        print("Stop requested; active transaction completed safely.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Linux Defragger Apple filesystem engine")
    parser.add_argument("operation", choices=("defrag", "compact", "recover"))
    parser.add_argument("device")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--live-map-cells", type=int)
    parser.add_argument("--ram-buffer")
    parser.add_argument("--workers")
    parser.add_argument("--transaction-files")
    args = parser.parse_args()
    try:
        with open(args.device, "rb", buffering=0) as probe:
            probe.seek(1024)
            signature = probe.read(2)
    except OSError as exc:
        raise Error(f"cannot probe Apple filesystem: {exc}") from exc
    if signature == b"BD":
        candidates = (Path(__file__).resolve().with_name("hfs_engine"),
                      Path("/usr/lib/linux-defragger/hfs_engine"))
        helper = next((item for item in candidates if item.is_file() and os.access(item, os.X_OK)), None)
        if helper is None:
            raise Error("classic HFS engine is unavailable")
        os.execv(str(helper), [str(helper), *sys.argv[1:]])
    if not args.write or args.confirm != args.device:
        raise Error("write confirmation required")
    if args.operation == "recover":
        recover(args.device, args.journal)
    else:
        operation(args.device, args.operation, args.journal, args.max_files,
                  args.live_map_cells)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Error as exc:
        print(f"apple-engine: {exc}", file=sys.stderr)
        raise SystemExit(1)
