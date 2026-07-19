# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""exFAT backend declaration and exact allocation-map integration."""

from __future__ import annotations

import math

from .base import (
    BackendError, BackendInfo, CAP_ANALYSE, CAP_COMPACT, CAP_DEFRAG, CAP_GROWTH_DEFRAG,
    CAP_MAP, CAP_RECOVER, FilesystemBackend, Reader, aggregate_bitmap, operation, u16le, u32le,
    u64le,
)

INFO = BackendInfo(
    "exfat", "exFAT", ("exfat",),
    CAP_ANALYSE | CAP_MAP | CAP_COMPACT | CAP_DEFRAG | CAP_RECOVER | CAP_GROWTH_DEFRAG,
    "exact",
    (
        operation("compact", "exfat"),
        operation("defrag", "exfat"),
        operation("growth-defrag", "exfat"),
        operation("recover", "exfat"),
    ),
)

class ExfatBackend(FilesystemBackend):
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            return r.read(3, 8) == b"EXFAT   "

    @staticmethod
    def _fragments(clusters: list[int]) -> int:
        """Return the number of physical extents in an exFAT cluster list."""
        if not clusters:
            return 0
        return 1 + sum(1 for left, right in zip(clusters, clusters[1:]) if right != left + 1)

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            bs = r.read(0, 512)
            if bs[3:11] != b"EXFAT   ":
                raise BackendError("not an exFAT volume")
            bps = 1 << bs[108]
            spc = 1 << bs[109]
            cluster_size = bps * spc
            fat_offset = u32le(bs, 80)
            fat_length = u32le(bs, 84)
            heap_offset = u32le(bs, 88)
            cluster_count = u32le(bs, 92)
            root_cluster = u32le(bs, 96)
            volume_serial = u32le(bs, 100)
            if cluster_count < 1 or root_cluster < 2:
                raise BackendError("invalid exFAT geometry")

            def cluster_off(cluster: int) -> int:
                if cluster < 2 or cluster >= cluster_count + 2:
                    raise BackendError(f"invalid exFAT cluster {cluster}")
                return (heap_offset + (cluster - 2) * spc) * bps

            fat = r.read(fat_offset * bps, fat_length * bps)

            def fat_next(cluster: int) -> int:
                off = cluster * 4
                if off + 4 > len(fat):
                    raise BackendError("exFAT FAT chain exceeds FAT")
                return u32le(fat, off) & 0xFFFFFFFF

            def chain(first: int, count: int | None = None, contiguous: bool = False) -> list[int]:
                if first == 0:
                    return []
                if contiguous:
                    if count is None:
                        raise BackendError("contiguous stream length is missing")
                    end = first + count
                    if first < 2 or end > cluster_count + 2:
                        raise BackendError("contiguous exFAT stream exceeds the cluster heap")
                    return list(range(first, end))
                result: list[int] = []
                seen: set[int] = set()
                current = first
                while 2 <= current < 0xFFFFFFF8:
                    if current in seen:
                        raise BackendError("exFAT FAT loop")
                    if current >= cluster_count + 2:
                        raise BackendError("exFAT FAT chain exceeds the cluster heap")
                    seen.add(current)
                    result.append(current)
                    if count is not None and len(result) >= count:
                        break
                    current = fat_next(current)
                if count is not None and len(result) != count:
                    raise BackendError("short exFAT FAT chain")
                return result

            def read_stream(clusters: list[int], length: int) -> bytes:
                output = bytearray()
                for cluster in clusters:
                    output += r.read(cluster_off(cluster), cluster_size)
                return bytes(output[:length])

            root_clusters = chain(root_cluster)
            root_data = bytearray(read_stream(root_clusters, len(root_clusters) * cluster_size))

            bitmap_cluster = None
            bitmap_length = None
            for off in range(0, len(root_data), 32):
                entry = root_data[off:off + 32]
                if len(entry) < 32 or entry[0] == 0x00:
                    break
                if entry[0] == 0x81:
                    bitmap_cluster = u32le(entry, 20)
                    bitmap_length = u64le(entry, 24)
                    if entry[1] & 1:
                        break
            if bitmap_cluster is None or bitmap_length is None:
                raise BackendError("exFAT allocation bitmap entry not found")

            bitmap_clusters = chain(bitmap_cluster, math.ceil(bitmap_length / cluster_size))
            bitmap = read_stream(bitmap_clusters, bitmap_length)
            if len(bitmap) * 8 < cluster_count:
                raise BackendError("exFAT allocation bitmap is shorter than the cluster heap")

            def allocated(cluster: int) -> bool:
                index = cluster - 2
                return bool((bitmap[index >> 3] >> (index & 7)) & 1)

            growth_10_satisfied = self._fragments(root_clusters) == 1
            regular_files = 0
            directories = 1  # Include the root directory, matching the FAT summary.
            fragmented_files = 0
            fragmented_directories = 1 if self._fragments(root_clusters) > 1 else 0
            fragmented_cluster_indices: set[int] = set()
            directory_cluster_indices: set[int] = {cluster - 2 for cluster in root_clusters}
            visited_directories: set[int] = {root_cluster}

            if fragmented_directories:
                fragmented_cluster_indices.update(cluster - 2 for cluster in root_clusters)

            def scan_directory(first: int, length: int, no_fat_chain: bool, display_path: str) -> None:
                nonlocal regular_files, directories, fragmented_files, fragmented_directories, growth_10_satisfied
                count = math.ceil(length / cluster_size) if length else 1
                clusters = chain(first, count, contiguous=no_fat_chain)
                data = bytearray(read_stream(clusters, len(clusters) * cluster_size))
                offset = 0
                while offset + 32 <= len(data):
                    entry_type = data[offset]
                    if entry_type == 0x00:
                        break
                    if entry_type != 0x85:
                        offset += 32
                        continue
                    secondary_count = data[offset + 1]
                    entry_count = 1 + secondary_count
                    if offset + entry_count * 32 > len(data):
                        raise BackendError("truncated exFAT directory entry set")
                    attributes = u16le(data, offset + 4)
                    stream = None
                    names: list[bytes] = []
                    for index in range(1, entry_count):
                        secondary = data[offset + index * 32:offset + (index + 1) * 32]
                        if secondary[0] == 0xC0:
                            stream = secondary
                        elif secondary[0] == 0xC1:
                            names.append(secondary[2:32])
                    if stream is not None:
                        name_length = stream[3]
                        raw_name = b"".join(names)[:name_length * 2]
                        name = raw_name.decode("utf-16le", "replace") or "<unnamed>"
                        child_first = u32le(stream, 20)
                        data_length = u64le(stream, 24)
                        child_no_fat = bool(stream[1] & 0x02)
                        is_directory = bool(attributes & 0x10)
                        child_count = math.ceil(data_length / cluster_size) if data_length else 0
                        child_clusters = chain(child_first, child_count, contiguous=child_no_fat) if child_count else []
                        extent_count = self._fragments(child_clusters)
                        if is_directory:
                            directories += 1
                            directory_cluster_indices.update(cluster - 2 for cluster in child_clusters)
                            if extent_count > 1:
                                fragmented_directories += 1
                                fragmented_cluster_indices.update(cluster - 2 for cluster in child_clusters)
                            if child_first and data_length and child_first not in visited_directories:
                                visited_directories.add(child_first)
                                child_path = f"{display_path}/{name}" if display_path else name
                                scan_directory(child_first, data_length, child_no_fat, child_path)
                        else:
                            regular_files += 1
                            if extent_count > 1:
                                fragmented_files += 1
                                fragmented_cluster_indices.update(cluster - 2 for cluster in child_clusters)
                                growth_10_satisfied = False
                            if child_clusters:
                                reserve = (len(child_clusters) * 10 + 99) // 100
                                cursor = child_clusters[-1] + 1
                                available = 0
                                while (available < reserve and cursor < cluster_count + 2
                                       and not allocated(cursor)):
                                    available += 1
                                    cursor += 1
                                if available < reserve:
                                    growth_10_satisfied = False
                    offset += entry_count * 32

            scan_directory(root_cluster, len(root_clusters) * cluster_size, False, "")

            result = aggregate_bitmap(
                bitmap,
                cluster_count,
                cells,
                cluster_size,
                "exfat",
                details={
                    "serial": f"{volume_serial:08x}",
                    "cluster_size": cluster_size,
                },
            )

            # Overlay directory and fragmented allocation counts onto the exact
            # bitmap map so exFAT uses the same colours and summary semantics as FAT.
            cell_count = int(result["cell_count"])
            for cluster_index in directory_cluster_indices:
                if 0 <= cluster_index < cluster_count:
                    cell_index = min(cell_count - 1, (cluster_index * cell_count) // cluster_count)
                    result["cells"][cell_index]["directory"] += 1
            for cluster_index in fragmented_cluster_indices:
                if 0 <= cluster_index < cluster_count:
                    cell_index = min(cell_count - 1, (cluster_index * cell_count) // cluster_count)
                    result["cells"][cell_index]["fragmented"] += 1

            result.update({
                "regular_files": regular_files,
                "directories": directories,
                "fragmented_files": fragmented_files,
                "fragmented_directories": fragmented_directories,
                "growth_10_satisfied": growth_10_satisfied and regular_files > 0,
            })
            return result

BACKEND = ExfatBackend()
