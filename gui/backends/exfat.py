# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""exFAT backend declaration and exact allocation-map integration."""

from __future__ import annotations
from .base import *

INFO = BackendInfo("exfat", "exFAT", ("exfat",), CAP_ANALYSE|CAP_MAP|CAP_COMPACT|CAP_DEFRAG|CAP_RECOVER, "exact")

class ExfatBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            return r.read(3, 8) == b"EXFAT   "

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
                return (heap_offset + (cluster - 2) * spc) * bps

            fat = r.read(fat_offset * bps, fat_length * bps)
            def fat_next(cluster: int) -> int:
                off = cluster * 4
                if off + 4 > len(fat):
                    raise BackendError("exFAT FAT chain exceeds FAT")
                return u32le(fat, off) & 0xFFFFFFFF

            root_data = bytearray()
            seen = set()
            cur = root_cluster
            while 2 <= cur < 0xFFFFFFF8 and cur not in seen:
                seen.add(cur)
                root_data += r.read(cluster_off(cur), cluster_size)
                cur = fat_next(cur)
                if len(root_data) > 64 * 1024 * 1024:
                    raise BackendError("exFAT root directory is implausibly large")

            bitmap_cluster = None
            bitmap_length = None
            for off in range(0, len(root_data), 32):
                entry = root_data[off:off+32]
                if len(entry) < 32 or entry[0] == 0x00:
                    break
                if entry[0] == 0x81:
                    bitmap_cluster = u32le(entry, 20)
                    bitmap_length = u64le(entry, 24)
                    if entry[1] & 1:
                        break
            if bitmap_cluster is None or bitmap_length is None:
                raise BackendError("exFAT allocation bitmap entry not found")

            bitmap = bytearray()
            cur = bitmap_cluster
            seen.clear()
            while len(bitmap) < bitmap_length and 2 <= cur < 0xFFFFFFF8 and cur not in seen:
                seen.add(cur)
                bitmap += r.read(cluster_off(cur), cluster_size)
                cur = fat_next(cur)
            bitmap = bitmap[:bitmap_length]
            if len(bitmap) * 8 < cluster_count:
                raise BackendError("exFAT allocation bitmap is shorter than the cluster heap")
            return aggregate_bitmap(bytes(bitmap), cluster_count, cells, cluster_size, "exfat",
                                    details={"serial": f"{volume_serial:08x}", "cluster_size": cluster_size})

BACKEND = ExfatBackend()
