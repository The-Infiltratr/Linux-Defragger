# Linux Defragger
# Author: Shannon Smith
# Purpose: Genuine read-only XFS allocation and fragmentation analysis.
#
# The analyser walks XFS allocation-group free-space and inode B+trees and
# decodes each allocated inode's data fork.  It never calls xfsprogs and never
# opens the volume for writing.

"""Read-only XFS allocation-group and inode-extent backend."""

from __future__ import annotations

import stat
from dataclasses import dataclass

from .base import (
    BackendError, BackendInfo, CAP_ANALYSE, CAP_COMPACT, CAP_MAP, FilesystemBackend, Reader,
    aggregate_ranges, complement_ranges, merge_ranges, operation, overlay_ranges, u16be, u32be,
    u64be,
)

INFO = BackendInfo(
    "xfs", "XFS", ("xfs",),
    CAP_ANALYSE | CAP_MAP | CAP_COMPACT,
    "exact",
    (
        operation(
            "compact",
            "linux-compact",
            pass_filesystem=True,
            warning=(
                "XFS Compact privately mounts the volume, reserves the free map and atomically "
                "swaps complete movable files into lower ranges. XFS metadata and unsupported "
                "special mappings remain fixed."
            ),
        ),
    ),
)

_XFS_SB_MAGIC = b"XFSB"
_XFS_AGF_MAGIC = b"XAGF"
_XFS_AGI_MAGIC = b"XAGI"
_XFS_ABTB_MAGIC = b"ABTB"
_XFS_ABTB_CRC_MAGIC = b"AB3B"
_XFS_IBT_MAGIC = b"IABT"
_XFS_IBT_CRC_MAGIC = b"IAB3"
_XFS_BMAP_MAGIC = b"BMAP"
_XFS_BMAP_CRC_MAGIC = b"BMA3"
_XFS_DINODE_MAGIC = 0x494E

_XFS_DINODE_FMT_LOCAL = 1
_XFS_DINODE_FMT_EXTENTS = 2
_XFS_DINODE_FMT_BTREE = 3
_XFS_DIFLAG_REALTIME = 0x0001
_XFS_DIFLAG2_NREXT64 = 1 << 4
_XFS_SB_FEAT_INCOMPAT_SPINODES = 1 << 1

_MAX_BTREE_LEVEL = 16
_MAX_BTREE_BLOCKS = 8_000_000
_MAX_INODES = 100_000_000
_MAX_EXTENTS_PER_INODE = 16_000_000


@dataclass(frozen=True)
class _Extent:
    logical: int
    physical: int
    length: int
    unwritten: bool = False








def _decode_bmbt_record(data: bytes, offset: int, agblocks: int,
                        agblklog: int, dblocks: int) -> _Extent:
    if offset < 0 or offset + 16 > len(data):
        raise BackendError("truncated XFS extent record")
    l0 = u64be(data, offset)
    l1 = u64be(data, offset + 8)
    unwritten = bool(l0 >> 63)
    logical = (l0 & ((1 << 63) - 1)) >> 9
    fsblock = ((l0 & 0x1FF) << 43) | (l1 >> 21)
    length = l1 & ((1 << 21) - 1)
    agno = fsblock >> agblklog
    agbno = fsblock & ((1 << agblklog) - 1)
    physical = agno * agblocks + agbno
    if length <= 0 or physical < 0 or physical + length > dblocks or agbno >= agblocks:
        raise BackendError("XFS extent points outside the data device")
    return _Extent(logical, physical, length, unwritten)


def _coalesce_extents(extents: list[_Extent]) -> list[_Extent]:
    output: list[_Extent] = []
    previous_logical_end = -1
    for extent in sorted(extents, key=lambda item: item.logical):
        if extent.logical < previous_logical_end:
            raise BackendError("overlapping XFS file extents")
        if output:
            old = output[-1]
            if (old.logical + old.length == extent.logical
                    and old.physical + old.length == extent.physical
                    and old.unwritten == extent.unwritten):
                output[-1] = _Extent(old.logical, old.physical, old.length + extent.length,
                                     old.unwritten)
            else:
                output.append(extent)
        else:
            output.append(extent)
        previous_logical_end = extent.logical + extent.length
    return output


class _XfsGeometry:
    def __init__(self, superblock: bytes, reader_size: int):
        self.block_size = u32be(superblock, 4)
        self.dblocks = u64be(superblock, 8)
        self.rblocks = u64be(superblock, 16)
        self.agblocks = u32be(superblock, 84)
        self.agcount = u32be(superblock, 88)
        self.version = u16be(superblock, 100) & 0xF
        self.sector_size = u16be(superblock, 102)
        self.inode_size = u16be(superblock, 104)
        self.inopblock = u16be(superblock, 106)
        self.blocklog = superblock[120]
        self.sectlog = superblock[121]
        self.inodelog = superblock[122]
        self.inopblog = superblock[123]
        self.agblklog = superblock[124]
        self.icount = u64be(superblock, 128)
        self.ifree = u64be(superblock, 136)
        self.fdblocks = u64be(superblock, 144)
        self.incompat = u32be(superblock, 216) if len(superblock) >= 220 else 0
        self.v5 = self.version == 5
        self.sparse_inodes = bool(self.incompat & _XFS_SB_FEAT_INCOMPAT_SPINODES)

        if self.block_size < 512 or self.block_size > 65536 or self.block_size & (self.block_size - 1):
            raise BackendError("unsupported XFS block size")
        if self.dblocks <= 0 or (reader_size > 0 and self.dblocks * self.block_size > reader_size):
            raise BackendError("invalid XFS data-device size")
        if self.agblocks <= 0 or self.agcount <= 0:
            raise BackendError("invalid XFS allocation-group geometry")
        if self.sector_size < 512 or self.sector_size > self.block_size or self.sector_size & (self.sector_size - 1):
            raise BackendError("unsupported XFS sector size")
        if self.inode_size < 256 or self.inode_size > self.block_size or self.inode_size & (self.inode_size - 1):
            raise BackendError("unsupported XFS inode size")
        if self.inopblock != self.block_size // self.inode_size:
            raise BackendError("inconsistent XFS inode geometry")
        if (1 << self.blocklog) != self.block_size or (1 << self.inopblog) != self.inopblock:
            raise BackendError("inconsistent XFS logarithmic geometry")
        if self.agblklog <= 0 or self.agblklog > 32:
            raise BackendError("unsupported XFS allocation-group address width")
        if self.rblocks:
            # The data-device map remains exact, but realtime-file data cannot be
            # represented without the separate realtime device.
            self.has_realtime_device = True
        else:
            self.has_realtime_device = False

    def ag_length(self, agno: int) -> int:
        start = agno * self.agblocks
        return max(0, min(self.agblocks, self.dblocks - start))

    def ag_offset(self, agno: int, agbno: int = 0) -> int:
        return (agno * self.agblocks + agbno) * self.block_size


class _ShortBtreeWalker:
    """Walk allocation-group short-pointer B+trees."""

    def __init__(self, reader: Reader, geometry: _XfsGeometry, agno: int,
                 magics: tuple[bytes, bytes], key_size: int, ptr_size: int, record_size: int):
        self.reader = reader
        self.g = geometry
        self.agno = agno
        self.magics = magics
        self.key_size = key_size
        self.ptr_size = ptr_size
        self.record_size = record_size
        self.blocks_read = 0

    def leaves(self, root: int) -> list[bytes]:
        if root <= 0 or root >= self.g.ag_length(self.agno):
            raise BackendError("XFS B+tree root points outside its allocation group")
        output: list[bytes] = []
        stack = [root]
        visited: set[int] = set()
        while stack:
            agbno = stack.pop()
            if agbno in visited:
                raise BackendError("loop in XFS allocation-group B+tree")
            visited.add(agbno)
            raw = self.reader.read(self.g.ag_offset(self.agno, agbno), self.g.block_size)
            if raw[:4] not in self.magics:
                raise BackendError("invalid XFS allocation-group B+tree magic")
            crc = raw[:4] == self.magics[1]
            header = 56 if crc else 16
            level = u16be(raw, 4)
            nrecs = u16be(raw, 6)
            if level > _MAX_BTREE_LEVEL:
                raise BackendError("invalid XFS B+tree level")
            self.blocks_read += 1
            if self.blocks_read > _MAX_BTREE_BLOCKS:
                raise BackendError("XFS B+tree traversal exceeded the safety limit")
            if level == 0:
                if header + nrecs * self.record_size > self.g.block_size:
                    raise BackendError("truncated XFS B+tree leaf")
                output.append(raw[header:header + nrecs * self.record_size])
                continue
            maxrecs = (self.g.block_size - header) // (self.key_size + self.ptr_size)
            if nrecs <= 0 or nrecs > maxrecs:
                raise BackendError("invalid XFS B+tree node record count")
            ptr_base = header + maxrecs * self.key_size
            children = []
            for index in range(nrecs):
                pos = ptr_base + index * self.ptr_size
                child = u32be(raw, pos) if self.ptr_size == 4 else u64be(raw, pos)
                if child <= 0 or child >= self.g.ag_length(self.agno):
                    raise BackendError("XFS B+tree child points outside its allocation group")
                children.append(child)
            stack.extend(reversed(children))
        return output


class _BmapWalker:
    """Decode direct and external XFS data-fork extent trees."""

    def __init__(self, reader: Reader, geometry: _XfsGeometry):
        self.reader = reader
        self.g = geometry
        self.blocks_read = 0

    def _external(self, fsblock: int, expected_level: int | None = None) -> list[_Extent]:
        agno = fsblock >> self.g.agblklog
        agbno = fsblock & ((1 << self.g.agblklog) - 1)
        physical = agno * self.g.agblocks + agbno
        if agno >= self.g.agcount or agbno >= self.g.ag_length(agno) or physical >= self.g.dblocks:
            raise BackendError("XFS bmap B+tree block points outside the filesystem")
        raw = self.reader.read(physical * self.g.block_size, self.g.block_size)
        if raw[:4] not in (_XFS_BMAP_MAGIC, _XFS_BMAP_CRC_MAGIC):
            raise BackendError("invalid XFS bmap B+tree magic")
        header = 72 if raw[:4] == _XFS_BMAP_CRC_MAGIC else 24
        level = u16be(raw, 4)
        nrecs = u16be(raw, 6)
        if level > _MAX_BTREE_LEVEL or (expected_level is not None and level != expected_level):
            raise BackendError("invalid XFS bmap tree level")
        self.blocks_read += 1
        if self.blocks_read > _MAX_BTREE_BLOCKS:
            raise BackendError("XFS bmap traversal exceeded the safety limit")
        if level == 0:
            if header + nrecs * 16 > self.g.block_size:
                raise BackendError("truncated XFS bmap leaf")
            return [_decode_bmbt_record(raw, header + index * 16, self.g.agblocks,
                                        self.g.agblklog, self.g.dblocks)
                    for index in range(nrecs)]
        maxrecs = (self.g.block_size - header) // 16
        if nrecs <= 0 or nrecs > maxrecs:
            raise BackendError("invalid XFS bmap node record count")
        ptr_base = header + maxrecs * 8
        extents: list[_Extent] = []
        for index in range(nrecs):
            child = u64be(raw, ptr_base + index * 8)
            extents.extend(self._external(child, level - 1))
        return extents

    def inode_fork(self, inode: bytes, core_size: int, fork_size: int,
                   data_format: int, nextents: int) -> list[_Extent]:
        if fork_size < 0 or core_size + fork_size > len(inode):
            raise BackendError("invalid XFS inode fork geometry")
        fork = inode[core_size:core_size + fork_size]
        if data_format == _XFS_DINODE_FMT_LOCAL:
            return []
        if data_format == _XFS_DINODE_FMT_EXTENTS:
            if nextents > _MAX_EXTENTS_PER_INODE or nextents * 16 > len(fork):
                raise BackendError("invalid XFS direct extent count")
            return _coalesce_extents([
                _decode_bmbt_record(fork, index * 16, self.g.agblocks,
                                    self.g.agblklog, self.g.dblocks)
                for index in range(nextents)
            ])
        if data_format != _XFS_DINODE_FMT_BTREE:
            raise BackendError("unsupported XFS inode data-fork format")
        if len(fork) < 4:
            raise BackendError("truncated XFS inode bmap root")
        level = u16be(fork, 0)
        nrecs = u16be(fork, 2)
        if level <= 0 or level > _MAX_BTREE_LEVEL:
            raise BackendError("invalid XFS inode bmap root level")
        maxrecs = (len(fork) - 4) // 16
        if nrecs <= 0 or nrecs > maxrecs:
            raise BackendError("invalid XFS inode bmap root count")
        ptr_base = 4 + maxrecs * 8
        extents: list[_Extent] = []
        for index in range(nrecs):
            child = u64be(fork, ptr_base + index * 8)
            extents.extend(self._external(child, level - 1))
        extents = _coalesce_extents(extents)
        if nextents and len(extents) > nextents:
            raise BackendError("XFS inode extent tree exceeds its recorded extent count")
        return extents


def _free_space(reader: Reader, g: _XfsGeometry) -> tuple[list[tuple[int, int]], list[dict], int]:
    free_ranges: list[tuple[int, int]] = []
    ag_details: list[dict] = []
    btree_blocks = 0
    for agno in range(g.agcount):
        ag_len = g.ag_length(agno)
        if ag_len <= 0:
            break
        agf = reader.read(g.ag_offset(agno) + g.sector_size, g.sector_size)
        if agf[:4] != _XFS_AGF_MAGIC or u32be(agf, 8) != agno:
            raise BackendError(f"invalid XFS AGF header for allocation group {agno}")
        recorded_len = u32be(agf, 12)
        root = u32be(agf, 16)
        freeblks = u32be(agf, 52)
        longest = u32be(agf, 56)
        if recorded_len != ag_len:
            raise BackendError(f"XFS allocation group {agno} has inconsistent length")
        walker = _ShortBtreeWalker(
            reader, g, agno, (_XFS_ABTB_MAGIC, _XFS_ABTB_CRC_MAGIC), 8, 4, 8
        )
        records = walker.leaves(root)
        btree_blocks += walker.blocks_read
        actual_free = 0
        previous_end = -1
        extents = 0
        for block in records:
            for pos in range(0, len(block), 8):
                start = u32be(block, pos)
                length = u32be(block, pos + 4)
                if length <= 0 or start < 0 or start + length > ag_len or start < previous_end:
                    raise BackendError(f"invalid XFS free-space record in allocation group {agno}")
                free_ranges.append((agno * g.agblocks + start, agno * g.agblocks + start + length))
                actual_free += length
                extents += 1
                previous_end = start + length
        ag_details.append({
            "ag": agno,
            "blocks": ag_len,
            "free_blocks": actual_free,
            "recorded_free_blocks": freeblks,
            "longest_free_extent": longest,
            "free_extents": extents,
            "free_count_matches_agf": actual_free == freeblks,
        })
    return merge_ranges(free_ranges), ag_details, btree_blocks


def _inode_records(reader: Reader, g: _XfsGeometry, agno: int) -> tuple[list[tuple[int, int, int]], int]:
    agi = reader.read(g.ag_offset(agno) + 2 * g.sector_size, g.sector_size)
    if agi[:4] != _XFS_AGI_MAGIC or u32be(agi, 8) != agno:
        raise BackendError(f"invalid XFS AGI header for allocation group {agno}")
    root = u32be(agi, 20)
    walker = _ShortBtreeWalker(
        reader, g, agno, (_XFS_IBT_MAGIC, _XFS_IBT_CRC_MAGIC), 4, 4, 16
    )
    leaves = walker.leaves(root)
    result: list[tuple[int, int, int]] = []
    for block in leaves:
        for pos in range(0, len(block), 16):
            startino = u32be(block, pos)
            if g.sparse_inodes:
                holemask = u16be(block, pos + 4)
                count = block[pos + 6]
            else:
                holemask = 0
                count = 64
            free_mask = u64be(block, pos + 8)
            if count > 64:
                raise BackendError("invalid XFS inode-chunk count")
            result.append((startino, holemask, free_mask))
    return result, walker.blocks_read


def _scan_inodes(reader: Reader, g: _XfsGeometry) -> dict:
    regular_files = directories = fragmented_files = fragmented_directories = 0
    inodes_scanned = malformed_inodes = realtime_inodes = 0
    fragmented_ranges: list[tuple[int, int]] = []
    directory_ranges: list[tuple[int, int]] = []
    inobt_blocks = 0
    bmap = _BmapWalker(reader, g)

    estimated_allocated = max(0, g.icount - g.ifree)
    if estimated_allocated > _MAX_INODES:
        raise BackendError("XFS allocated-inode count exceeds the native analyser safety limit")

    for agno in range(g.agcount):
        records, blocks = _inode_records(reader, g, agno)
        inobt_blocks += blocks
        ag_len = g.ag_length(agno)
        for startino, holemask, free_mask in records:
            for index in range(64):
                if holemask & (1 << (index // 4)):
                    continue
                if free_mask & (1 << index):
                    continue
                agino = startino + index
                agbno = agino >> g.inopblog
                inode_index = agino & (g.inopblock - 1)
                if agbno >= ag_len:
                    malformed_inodes += 1
                    continue
                offset = g.ag_offset(agno, agbno) + inode_index * g.inode_size
                inode = reader.read(offset, g.inode_size)
                if u16be(inode, 0) != _XFS_DINODE_MAGIC:
                    malformed_inodes += 1
                    continue
                mode = u16be(inode, 2)
                file_type = stat.S_IFMT(mode)
                if file_type not in (stat.S_IFREG, stat.S_IFDIR):
                    continue
                version = inode[4]
                data_format = inode[5]
                if version not in (1, 2, 3):
                    malformed_inodes += 1
                    continue
                core_size = 176 if version == 3 else 100
                forkoff = inode[82]
                fork_end = forkoff * 8 if forkoff else g.inode_size
                fork_size = fork_end - core_size
                flags = u16be(inode, 90)
                flags2 = u64be(inode, 120) if version == 3 else 0
                if flags & _XFS_DIFLAG_REALTIME:
                    realtime_inodes += 1
                    if file_type == stat.S_IFREG:
                        regular_files += 1
                    else:
                        directories += 1
                    continue
                nextents = u64be(inode, 24) if flags2 & _XFS_DIFLAG2_NREXT64 else u32be(inode, 76)
                inodes_scanned += 1
                if file_type == stat.S_IFREG:
                    regular_files += 1
                else:
                    directories += 1
                try:
                    extents = bmap.inode_fork(inode, core_size, fork_size, data_format, nextents)
                except (BackendError, ValueError):
                    malformed_inodes += 1
                    continue
                physical = [(extent.physical, extent.physical + extent.length) for extent in extents]
                fragmented = len(extents) > 1
                if file_type == stat.S_IFDIR:
                    directory_ranges.extend(physical)
                    if fragmented:
                        fragmented_directories += 1
                        fragmented_ranges.extend(physical)
                elif fragmented:
                    fragmented_files += 1
                    fragmented_ranges.extend(physical)

    return {
        "regular_files": regular_files,
        "directories": directories,
        "fragmented_files": fragmented_files,
        "fragmented_directories": fragmented_directories,
        "fragmentation_percent": 100.0 * fragmented_files / max(1, regular_files),
        "inodes_scanned": inodes_scanned,
        "malformed_inodes": malformed_inodes,
        "realtime_inodes": realtime_inodes,
        "inobt_blocks": inobt_blocks,
        "bmap_blocks": bmap.blocks_read,
        "fragmented_ranges": merge_ranges(fragmented_ranges),
        "directory_ranges": merge_ranges(directory_ranges),
    }


class XfsBackend(FilesystemBackend):
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            return reader.read(0, 4) == _XFS_SB_MAGIC

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            superblock = reader.read(0, 512)
            if superblock[:4] != _XFS_SB_MAGIC:
                raise BackendError("not an XFS volume")
            g = _XfsGeometry(superblock, reader.size)
            free_ranges, ag_details, bnobt_blocks = _free_space(reader, g)
            used_ranges = complement_ranges(g.dblocks, free_ranges)
            ranges = [(start, end, 0) for start, end in free_ranges]
            ranges.extend((start, end, 1) for start, end in used_ranges)
            result = aggregate_ranges(
                g.dblocks,
                cells,
                g.block_size,
                "xfs",
                ranges,
                "exact",
                {
                    "block_size": g.block_size,
                    "sector_size": g.sector_size,
                    "inode_size": g.inode_size,
                    "allocation_group_blocks": g.agblocks,
                    "allocation_groups": ag_details,
                    "bnobt_blocks": bnobt_blocks,
                    "superblock_free_blocks": g.fdblocks,
                    "free_space_basis": "XFS per-allocation-group block-number free-space B+trees",
                },
            )

            try:
                summary = _scan_inodes(reader, g)
            except BackendError as exc:
                result["details"]["fragmentation_available"] = False
                result["details"]["fragmentation_note"] = str(exc)
                return result

            fragmented_blocks = overlay_ranges(
                result["cells"], summary["fragmented_ranges"], "fragmented"
            )
            directory_blocks = overlay_ranges(
                result["cells"], summary["directory_ranges"], "directory"
            )
            result.update({
                "regular_files": summary["regular_files"],
                "directories": summary["directories"],
                "fragmented_files": summary["fragmented_files"],
                "fragmented_directories": summary["fragmented_directories"],
                "fragmentation_percent": summary["fragmentation_percent"],
            })
            result["details"].update({
                "fragmentation_available": True,
                "fragmentation_basis": "XFS inode B+trees and data-fork extent maps",
                "inodes_scanned": summary["inodes_scanned"],
                "malformed_inodes": summary["malformed_inodes"],
                "realtime_inodes_not_mapped": summary["realtime_inodes"],
                "inobt_blocks": summary["inobt_blocks"],
                "bmap_blocks": summary["bmap_blocks"],
                "fragmented_blocks_mapped": fragmented_blocks,
                "directory_blocks_mapped": directory_blocks,
            })
            return result


BACKEND = XfsBackend()
