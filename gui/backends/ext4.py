# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Read-only ext2, ext3 and ext4 allocation-bitmap backend."""

from __future__ import annotations
from .base import *

INFO = BackendInfo("ext4", "ext2/3/4", ("ext2", "ext3", "ext4"), CAP_ANALYSE|CAP_MAP, "exact")

class ExtBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            sb = r.read(1024, 1024)
            return u16le(sb, 56) == 0xEF53

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            sb = r.read(1024, 1024)
            if u16le(sb, 56) != 0xEF53:
                raise BackendError("not an ext filesystem")
            block_size = 1024 << u32le(sb, 24)
            blocks_lo = u32le(sb, 4)
            incompat = u32le(sb, 96)
            has_64bit = bool(incompat & 0x80)
            blocks_hi = u32le(sb, 0x150) if has_64bit else 0
            total_blocks = blocks_lo | (blocks_hi << 32)
            first_data = u32le(sb, 20)
            blocks_per_group = u32le(sb, 32)
            desc_size = u16le(sb, 0xFE) if has_64bit else 32
            desc_size = max(32, desc_size)
            if not total_blocks or not blocks_per_group:
                raise BackendError("invalid ext geometry")
            groups = (total_blocks - first_data + blocks_per_group - 1) // blocks_per_group
            desc_table_block = 2 if block_size == 1024 else 1
            desc_table_off = desc_table_block * block_size
            bitmaps: list[bytes | None] = []
            group_lengths: list[int] = []
            for group in range(groups):
                desc = r.read(desc_table_off + group * desc_size, desc_size)
                bitmap_block = u32le(desc, 0)
                if has_64bit and desc_size >= 36:
                    bitmap_block |= u32le(desc, 32) << 32
                group_start = first_data + group * blocks_per_group
                group_count = min(blocks_per_group, total_blocks - group_start)
                group_lengths.append(group_count)
                bitmaps.append(r.read(bitmap_block * block_size, block_size) if bitmap_block else None)

            cell_count = max(1, min(cells, total_blocks))
            out = []
            free_total = used_total = unknown_total = 0
            for ci in range(cell_count):
                start = (ci * total_blocks) // cell_count
                end_ex = ((ci + 1) * total_blocks) // cell_count
                free = used = unknown = 0
                pos = start
                if pos < first_data:
                    n = min(end_ex, first_data) - pos
                    unknown += n
                    pos += n
                while pos < end_ex:
                    rel = pos - first_data
                    group = rel // blocks_per_group
                    within = rel % blocks_per_group
                    take = min(end_ex - pos, group_lengths[group] - within)
                    bitmap = bitmaps[group]
                    if bitmap is None:
                        unknown += take
                    else:
                        set_bits = count_set_bits(bitmap, within, within + take)
                        used += set_bits
                        free += take - set_bits
                    pos += take
                free_total += free; used_total += used; unknown_total += unknown
                out.append({"start": start, "end": end_ex - 1, "free": free, "used": used,
                            "unknown": unknown, "bad": 0, "fragmented": 0, "directory": 0})
            return {"schema": 1, "backend": "read-only-domain", "filesystem": "ext4",
                    "map_accuracy": "exact", "unit_size": block_size, "total_units": total_blocks,
                    "cell_count": cell_count, "total_bytes": total_blocks * block_size,
                    "free_bytes": free_total * block_size, "used_bytes": used_total * block_size,
                    "unknown_bytes": unknown_total * block_size, "cells": out,
                    "details": {"block_size": block_size, "groups": groups}}

BACKEND = ExtBackend()
