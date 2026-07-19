# Linux Defragger
# Author: Shannon Smith
# Purpose: Genuine read-only Btrfs physical allocation and fragmentation analysis.
#
# The analyser walks Btrfs' chunk, root, extent and filesystem trees directly.
# It does not call btrfs-progs and never opens the volume for writing.

"""Read-only single-device Btrfs allocation and fragmentation backend."""

from __future__ import annotations

import bisect
import stat
from dataclasses import dataclass

from .base import *

INFO = BackendInfo("btrfs", "Btrfs", ("btrfs",), CAP_ANALYSE | CAP_MAP | CAP_COMPACT, "exact-single-device")

_BTRFS_MAGIC = b"_BHRfS_M"
_SUPER_OFFSET = 64 * 1024
_SUPER_SIZE = 4096
_HEADER_SIZE = 101
_ITEM_SIZE = 25
_KEY_PTR_SIZE = 33
_DISK_KEY_SIZE = 17
_CHUNK_FIXED_SIZE = 48
_STRIPE_SIZE = 32

# Key types and well-known tree object IDs from linux/btrfs_tree.h.
_INODE_ITEM = 1
_EXTENT_DATA = 108
_ROOT_ITEM = 132
_EXTENT_ITEM = 168
_METADATA_ITEM = 169
_CHUNK_ITEM = 228
_EXTENT_TREE_OBJECTID = 2
_FS_TREE_OBJECTID = 5
_FIRST_FREE_OBJECTID = 256

_FILE_EXTENT_INLINE = 0
_FILE_EXTENT_REG = 1
_FILE_EXTENT_PREALLOC = 2

# Chunk profile bits. Single, DUP and same-device RAID1 mirrors are directly
# representable on one block device. Striping profiles require a stripe-aware
# byte mapper and are rejected rather than producing a plausible but false map.
_BLOCK_GROUP_DATA = 1
_BLOCK_GROUP_SYSTEM = 2
_BLOCK_GROUP_METADATA = 4
_BLOCK_GROUP_RAID0 = 8
_BLOCK_GROUP_RAID1 = 16
_BLOCK_GROUP_DUP = 32
_BLOCK_GROUP_RAID10 = 64
_BLOCK_GROUP_RAID5 = 128
_BLOCK_GROUP_RAID6 = 256
_BLOCK_GROUP_RAID1C3 = 512
_BLOCK_GROUP_RAID1C4 = 1024
_STRIPED_PROFILES = _BLOCK_GROUP_RAID0 | _BLOCK_GROUP_RAID10 | _BLOCK_GROUP_RAID5 | _BLOCK_GROUP_RAID6

_MAX_TREE_LEVEL = 8
_MAX_TREE_BLOCKS = 8_000_000


@dataclass(frozen=True)
class _Key:
    objectid: int
    type: int
    offset: int


@dataclass(frozen=True)
class _Chunk:
    logical: int
    length: int
    chunk_type: int
    stripe_len: int
    stripes: tuple[tuple[int, int], ...]  # (devid, physical byte offset)

    @property
    def end(self) -> int:
        return self.logical + self.length


@dataclass(frozen=True)
class _TreeItem:
    key: _Key
    data: bytes


@dataclass(frozen=True)
class _FileRun:
    logical: int
    physical: int
    length: int
    disk_start: int
    disk_length: int
    encoded: bool


def _key(data: bytes, offset: int = 0) -> _Key:
    if offset < 0 or offset + _DISK_KEY_SIZE > len(data):
        raise BackendError("truncated Btrfs disk key")
    return _Key(u64le(data, offset), data[offset + 8], u64le(data, offset + 9))


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


def _complement(total: int, used: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = 0
    for start, end in _merge_ranges(used):
        start = max(0, min(total, start))
        end = max(0, min(total, end))
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        result.append((cursor, total))
    return result


def _overlay_ranges(cells: list[dict], ranges: list[tuple[int, int]], field: str) -> int:
    merged = _merge_ranges(ranges)
    index = 0
    total = sum(end - start for start, end in merged)
    for cell in cells:
        start = int(cell["start"])
        end_ex = int(cell["end"]) + 1
        while index < len(merged) and merged[index][1] <= start:
            index += 1
        overlap = 0
        check = index
        while check < len(merged) and merged[check][0] < end_ex:
            overlap += max(0, min(end_ex, merged[check][1]) - max(start, merged[check][0]))
            if merged[check][1] > end_ex:
                break
            check += 1
        cell[field] = min(int(cell.get("used", 0)), overlap)
    return total


def _parse_chunk(data: bytes, logical: int) -> _Chunk:
    if len(data) < _CHUNK_FIXED_SIZE:
        raise BackendError("truncated Btrfs chunk item")
    length = u64le(data, 0)
    stripe_len = u64le(data, 16)
    chunk_type = u64le(data, 24)
    num_stripes = u16le(data, 44)
    if length <= 0 or stripe_len <= 0 or num_stripes <= 0:
        raise BackendError("invalid Btrfs chunk geometry")
    if _CHUNK_FIXED_SIZE + num_stripes * _STRIPE_SIZE > len(data):
        raise BackendError("truncated Btrfs chunk stripes")
    stripes = []
    for index in range(num_stripes):
        pos = _CHUNK_FIXED_SIZE + index * _STRIPE_SIZE
        stripes.append((u64le(data, pos), u64le(data, pos + 8)))
    return _Chunk(logical, length, chunk_type, stripe_len, tuple(stripes))


class _Mapper:
    def __init__(self, chunks: list[_Chunk], devid: int, device_size: int):
        self.devid = devid
        self.device_size = device_size
        by_logical: dict[int, _Chunk] = {}
        for chunk in chunks:
            existing = by_logical.get(chunk.logical)
            if existing is not None and existing != chunk:
                raise BackendError("conflicting Btrfs chunk mappings")
            by_logical[chunk.logical] = chunk
        self.chunks = sorted(by_logical.values(), key=lambda item: item.logical)
        self.starts = [item.logical for item in self.chunks]
        previous_end = -1
        for chunk in self.chunks:
            if chunk.logical < previous_end:
                raise BackendError("overlapping Btrfs chunks")
            if chunk.chunk_type & _STRIPED_PROFILES:
                raise BackendError("striped Btrfs profiles are not yet supported by the native analyser")
            matching = [stripe for stripe in chunk.stripes if stripe[0] == devid]
            if not matching:
                raise BackendError("Btrfs chunk has no stripe on this device")
            for _devid, physical in matching:
                if physical < 0 or physical + chunk.length > device_size:
                    raise BackendError("Btrfs chunk stripe points outside the device")
            previous_end = chunk.end

    def _find(self, logical: int) -> _Chunk:
        index = bisect.bisect_right(self.starts, logical) - 1
        if index < 0:
            raise BackendError(f"Btrfs logical address {logical} has no chunk mapping")
        chunk = self.chunks[index]
        if logical >= chunk.end:
            raise BackendError(f"Btrfs logical address {logical} has no chunk mapping")
        return chunk

    def read_physical(self, logical: int, length: int) -> int:
        chunk = self._find(logical)
        if logical + length > chunk.end:
            raise BackendError("Btrfs tree block crosses a chunk boundary")
        for stripe_devid, stripe_offset in chunk.stripes:
            if stripe_devid == self.devid:
                return stripe_offset + (logical - chunk.logical)
        raise BackendError("Btrfs logical address is not stored on this device")

    def physical_ranges(self, logical: int, length: int) -> list[tuple[int, int]]:
        """Translate a logical byte range to every local physical mirror."""
        if length <= 0:
            return []
        result: list[tuple[int, int]] = []
        cursor = logical
        remaining = length
        while remaining:
            chunk = self._find(cursor)
            take = min(remaining, chunk.end - cursor)
            delta = cursor - chunk.logical
            local = 0
            for stripe_devid, stripe_offset in chunk.stripes:
                if stripe_devid == self.devid:
                    start = stripe_offset + delta
                    end = start + take
                    if end > self.device_size:
                        raise BackendError("Btrfs physical extent points outside the device")
                    result.append((start, end))
                    local += 1
            if not local:
                raise BackendError("Btrfs extent has no local physical mirror")
            cursor += take
            remaining -= take
        return result


class _TreeReader:
    def __init__(self, reader: Reader, mapper: _Mapper, nodesize: int):
        self.reader = reader
        self.mapper = mapper
        self.nodesize = nodesize
        self.blocks_read = 0

    def block(self, logical: int, expected_level: int | None = None) -> bytes:
        if logical <= 0 or logical % self.nodesize:
            raise BackendError("invalid Btrfs tree-block address")
        physical = self.mapper.read_physical(logical, self.nodesize)
        raw = self.reader.read(physical, self.nodesize)
        if u64le(raw, 48) != logical:
            raise BackendError("Btrfs tree-block bytenr mismatch")
        level = raw[100]
        if level > _MAX_TREE_LEVEL or (expected_level is not None and level != expected_level):
            raise BackendError("invalid Btrfs tree level")
        nritems = u32le(raw, 96)
        element_size = _ITEM_SIZE if level == 0 else _KEY_PTR_SIZE
        if _HEADER_SIZE + nritems * element_size > self.nodesize:
            raise BackendError("invalid Btrfs tree item count")
        self.blocks_read += 1
        if self.blocks_read > _MAX_TREE_BLOCKS:
            raise BackendError("Btrfs tree traversal exceeded the safety limit")
        return raw

    def walk(self, root: int, root_level: int) -> tuple[list[_TreeItem], list[int]]:
        items: list[_TreeItem] = []
        blocks: list[int] = []
        stack = [(root, root_level)]
        visited: set[int] = set()
        while stack:
            logical, expected_level = stack.pop()
            if logical in visited:
                continue
            visited.add(logical)
            raw = self.block(logical, expected_level)
            blocks.append(logical)
            level = raw[100]
            nritems = u32le(raw, 96)
            if level == 0:
                for index in range(nritems):
                    pos = _HEADER_SIZE + index * _ITEM_SIZE
                    item_key = _key(raw, pos)
                    # btrfs_item.offset is relative to the end of the 0x65-byte
                    # tree header, not to byte zero of the tree block.  Treating
                    # it as an absolute block offset works on a synthetic image
                    # built with the same mistake, but reads arbitrary bytes from
                    # genuine Btrfs leaves and corrupts every item parser.
                    relative_offset = u32le(raw, pos + 17)
                    data_size = u32le(raw, pos + 21)
                    data_offset = _HEADER_SIZE + relative_offset
                    item_table_end = _HEADER_SIZE + nritems * _ITEM_SIZE
                    if data_offset < item_table_end:
                        raise BackendError("Btrfs leaf item overlaps its item table")
                    if data_offset + data_size > self.nodesize:
                        raise BackendError("Btrfs leaf item lies outside the tree block")
                    items.append(_TreeItem(item_key, raw[data_offset:data_offset + data_size]))
            else:
                children: list[tuple[int, int]] = []
                for index in range(nritems):
                    pos = _HEADER_SIZE + index * _KEY_PTR_SIZE
                    child = u64le(raw, pos + 17)
                    if child <= 0:
                        raise BackendError("invalid Btrfs child pointer")
                    children.append((child, level - 1))
                # Reverse before pushing so items retain on-disk key order.
                stack.extend(reversed(children))
        return items, blocks


def _system_chunks(superblock: bytes) -> list[_Chunk]:
    size = u32le(superblock, 160)
    if size <= 0 or size > 2048:
        raise BackendError("invalid Btrfs system chunk array size")
    data = superblock[811:811 + size]
    chunks: list[_Chunk] = []
    pos = 0
    while pos < len(data):
        if pos + _DISK_KEY_SIZE + _CHUNK_FIXED_SIZE > len(data):
            raise BackendError("truncated Btrfs system chunk array")
        item_key = _key(data, pos)
        if item_key.type != _CHUNK_ITEM:
            raise BackendError("unexpected key in Btrfs system chunk array")
        num_stripes = u16le(data, pos + _DISK_KEY_SIZE + 44)
        item_size = _CHUNK_FIXED_SIZE + num_stripes * _STRIPE_SIZE
        end = pos + _DISK_KEY_SIZE + item_size
        if end > len(data):
            raise BackendError("truncated Btrfs system chunk item")
        chunks.append(_parse_chunk(data[pos + _DISK_KEY_SIZE:end], item_key.offset))
        pos = end
    return chunks


def _root_records(items: list[_TreeItem]) -> dict[int, tuple[int, int, int]]:
    """Return objectid -> (bytenr, level, refs), preferring the newest key."""
    roots: dict[int, tuple[int, int, int, int]] = {}
    for item in items:
        if item.key.type != _ROOT_ITEM or len(item.data) < 239:
            continue
        bytenr = u64le(item.data, 176)
        refs = u32le(item.data, 216)
        level = item.data[238]
        previous = roots.get(item.key.objectid)
        if bytenr and level <= _MAX_TREE_LEVEL and (previous is None or item.key.offset >= previous[3]):
            roots[item.key.objectid] = (bytenr, level, refs, item.key.offset)
    return {objectid: value[:3] for objectid, value in roots.items()}


def _extent_physical_ranges(items: list[_TreeItem], mapper: _Mapper) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for item in items:
        if item.key.type == _EXTENT_ITEM:
            length = item.key.offset
        elif item.key.type == _METADATA_ITEM:
            # Skinny metadata extent length equals the filesystem node size, but
            # mapper.physical_ranges receives that value from its caller.
            length = 0
        else:
            continue
        if length:
            ranges.extend(mapper.physical_ranges(item.key.objectid, length))
    return ranges


def _coalesce_file_runs(runs: list[_FileRun]) -> list[_FileRun]:
    output: list[_FileRun] = []
    for run in sorted(runs, key=lambda value: value.logical):
        if run.length <= 0:
            continue
        if output:
            old = output[-1]
            if (not old.encoded and not run.encoded and old.logical + old.length == run.logical
                    and old.physical + old.length == run.physical):
                output[-1] = _FileRun(old.logical, old.physical, old.length + run.length,
                                      old.disk_start, old.disk_length + run.disk_length, False)
                continue
        output.append(run)
    return output


def _scan_filesystem_trees(tree_reader: _TreeReader, roots: dict[int, tuple[int, int, int]],
                           mapper: _Mapper) -> dict:
    regular_files = directories = fragmented_files = 0
    malformed_items = 0
    fragmented_physical: list[tuple[int, int]] = []
    fs_tree_blocks = 0

    root_ids = [objectid for objectid, (_bytenr, _level, refs) in roots.items()
                if refs and (objectid == _FS_TREE_OBJECTID or objectid >= _FIRST_FREE_OBJECTID)]
    for objectid in sorted(root_ids):
        root, level, _refs = roots[objectid]
        try:
            items, blocks = tree_reader.walk(root, level)
        except BackendError:
            malformed_items += 1
            continue
        fs_tree_blocks += len(blocks)
        inode_modes: dict[int, int] = {}
        file_runs: dict[int, list[_FileRun]] = {}
        for item in items:
            if item.key.type == _INODE_ITEM and len(item.data) >= 56:
                inode_modes[item.key.objectid] = stat.S_IFMT(u32le(item.data, 52))
                continue
            if item.key.type != _EXTENT_DATA or len(item.data) < 21:
                continue
            extent_type = item.data[20]
            if extent_type == _FILE_EXTENT_INLINE:
                continue
            if extent_type not in (_FILE_EXTENT_REG, _FILE_EXTENT_PREALLOC) or len(item.data) < 53:
                malformed_items += 1
                continue
            disk_bytenr = u64le(item.data, 21)
            disk_num_bytes = u64le(item.data, 29)
            extent_offset = u64le(item.data, 37)
            num_bytes = u64le(item.data, 45)
            if disk_bytenr == 0 or num_bytes == 0:
                continue  # sparse hole
            encoded = bool(item.data[16] or item.data[17] or u16le(item.data, 18))
            try:
                if encoded:
                    # Encoded extents occupy their entire compressed disk range.
                    physical_ranges = mapper.physical_ranges(disk_bytenr, disk_num_bytes)
                    physical = physical_ranges[0][0]
                    disk_start = physical
                    disk_length = physical_ranges[0][1] - physical_ranges[0][0]
                else:
                    physical_ranges = mapper.physical_ranges(disk_bytenr + extent_offset, num_bytes)
                    physical = physical_ranges[0][0]
                    disk_start = physical
                    disk_length = num_bytes
            except BackendError:
                malformed_items += 1
                continue
            file_runs.setdefault(item.key.objectid, []).append(
                _FileRun(item.key.offset, physical, num_bytes, disk_start, disk_length, encoded)
            )

        for inode, mode in inode_modes.items():
            if mode == stat.S_IFREG:
                regular_files += 1
                runs = _coalesce_file_runs(file_runs.get(inode, []))
                if len(runs) > 1:
                    fragmented_files += 1
                    # Runs already store local physical positions.  For mirrored
                    # encoded data, highlighting the first local mirror is enough
                    # to identify the fragmented file without double counting it.
                    fragmented_physical.extend(
                        (run.disk_start, run.disk_start + run.disk_length) for run in runs
                    )
            elif mode == stat.S_IFDIR:
                directories += 1

    return {
        "regular_files": regular_files,
        "directories": directories,
        "fragmented_files": fragmented_files,
        "fragmented_directories": 0,
        "fragmentation_percent": 100.0 * fragmented_files / max(1, regular_files),
        "fragmented_ranges": _merge_ranges(fragmented_physical),
        "filesystem_roots_scanned": len(root_ids),
        "filesystem_tree_blocks": fs_tree_blocks,
        "malformed_items": malformed_items,
    }


class BtrfsBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            return reader.read(_SUPER_OFFSET + 0x40, 8) == _BTRFS_MAGIC

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            superblock = reader.read(_SUPER_OFFSET, _SUPER_SIZE)
            if superblock[0x40:0x48] != _BTRFS_MAGIC:
                raise BackendError("not a Btrfs volume")
            total_bytes = u64le(superblock, 112)
            bytes_used_logical = u64le(superblock, 120)
            num_devices = u64le(superblock, 136)
            sectorsize = u32le(superblock, 144)
            nodesize = u32le(superblock, 148)
            root = u64le(superblock, 80)
            chunk_root = u64le(superblock, 88)
            root_level = superblock[198]
            chunk_root_level = superblock[199]
            devid = u64le(superblock, 201)

            if num_devices != 1:
                raise BackendError(
                    "native exact Btrfs analysis currently supports single-device filesystems only"
                )
            if total_bytes <= 0 or (reader.size > 0 and total_bytes > reader.size):
                raise BackendError("invalid Btrfs device size")
            if sectorsize < 4096 or sectorsize & (sectorsize - 1):
                raise BackendError("unsupported Btrfs sector size")
            if nodesize < sectorsize or nodesize > 65536 or nodesize & (nodesize - 1):
                raise BackendError("unsupported Btrfs node size")
            if not root or not chunk_root or root_level > _MAX_TREE_LEVEL or chunk_root_level > _MAX_TREE_LEVEL:
                raise BackendError("invalid Btrfs tree roots")

            # The superblock's system chunk array is sufficient to locate the
            # chunk tree.  Walking that tree supplies all remaining mappings.
            chunks = _system_chunks(superblock)
            bootstrap = _Mapper(chunks, devid, total_bytes)
            chunk_reader = _TreeReader(reader, bootstrap, nodesize)
            chunk_items, chunk_blocks = chunk_reader.walk(chunk_root, chunk_root_level)
            for item in chunk_items:
                if item.key.type == _CHUNK_ITEM:
                    chunks.append(_parse_chunk(item.data, item.key.offset))
            mapper = _Mapper(chunks, devid, total_bytes)
            tree_reader = _TreeReader(reader, mapper, nodesize)

            root_items, root_blocks = tree_reader.walk(root, root_level)
            roots = _root_records(root_items)
            extent_record = roots.get(_EXTENT_TREE_OBJECTID)
            if extent_record is None:
                raise BackendError("Btrfs root tree does not contain the extent tree")
            extent_root, extent_level, _extent_refs = extent_record
            extent_items, extent_blocks = tree_reader.walk(extent_root, extent_level)

            used_bytes_ranges: list[tuple[int, int]] = []
            for item in extent_items:
                if item.key.type == _EXTENT_ITEM:
                    length = item.key.offset
                elif item.key.type == _METADATA_ITEM:
                    length = nodesize
                else:
                    continue
                if item.key.objectid and length:
                    used_bytes_ranges.extend(mapper.physical_ranges(item.key.objectid, length))

            # Superblock mirrors are outside the extent tree but are allocated
            # metadata.  Include each mirror that lies on this device.
            for mirror in (64 * 1024, 64 * 1024 * 1024, 256 * 1024**3):
                if mirror + _SUPER_SIZE <= total_bytes:
                    used_bytes_ranges.append((mirror, mirror + _SUPER_SIZE))

            used_units = _merge_ranges([
                (start // sectorsize, (end + sectorsize - 1) // sectorsize)
                for start, end in used_bytes_ranges
            ])
            total_units = (total_bytes + sectorsize - 1) // sectorsize
            used_units = [(max(0, start), min(total_units, end)) for start, end in used_units
                          if start < total_units and end > 0]
            free_units = _complement(total_units, used_units)
            ranges = [(start, end, 0) for start, end in free_units]
            ranges.extend((start, end, 1) for start, end in used_units)

            result = aggregate_ranges(
                total_units,
                cells,
                sectorsize,
                "btrfs",
                ranges,
                "exact-single-device",
                {
                    "sector_size": sectorsize,
                    "node_size": nodesize,
                    "device_id": devid,
                    "chunks": len(mapper.chunks),
                    "logical_bytes_used": bytes_used_logical,
                    "chunk_tree_blocks": len(chunk_blocks),
                    "root_tree_blocks": len(root_blocks),
                    "extent_tree_blocks": len(extent_blocks),
                },
            )

            try:
                summary = _scan_filesystem_trees(tree_reader, roots, mapper)
            except BackendError as exc:
                result["details"]["fragmentation_available"] = False
                result["details"]["fragmentation_note"] = str(exc)
                return result

            fragmented_units = _merge_ranges([
                (start // sectorsize, (end + sectorsize - 1) // sectorsize)
                for start, end in summary["fragmented_ranges"]
            ])
            fragmented_mapped = _overlay_ranges(result["cells"], fragmented_units, "fragmented")
            result.update({
                "regular_files": summary["regular_files"],
                "directories": summary["directories"],
                "fragmented_files": summary["fragmented_files"],
                "fragmented_directories": summary["fragmented_directories"],
                "fragmentation_percent": summary["fragmentation_percent"],
            })
            result["details"].update({
                "fragmentation_available": True,
                "fragmentation_basis": "Btrfs inode and FILE_EXTENT_ITEM records across live filesystem roots",
                "directory_fragmentation_note": (
                    "Btrfs directory records share filesystem-tree blocks and do not form private block chains"
                ),
                "filesystem_roots_scanned": summary["filesystem_roots_scanned"],
                "filesystem_tree_blocks": summary["filesystem_tree_blocks"],
                "malformed_items": summary["malformed_items"],
                "fragmented_sectors_mapped": fragmented_mapped,
            })
            return result


BACKEND = BtrfsBackend()
