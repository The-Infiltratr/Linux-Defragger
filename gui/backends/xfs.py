# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Conservative read-only XFS allocation-group summary backend."""

from __future__ import annotations
from .base import *

INFO = BackendInfo("xfs", "XFS", ("xfs",), CAP_ANALYSE|CAP_MAP, "allocation-group-summary")

class XfsBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            return r.read(0, 4) == b"XFSB"

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            sb = r.read(0, 512)
            if sb[:4] != b"XFSB":
                raise BackendError("not an XFS volume")
            block_size = u32be(sb, 4)
            dblocks = u64be(sb, 8)
            agblocks = u32be(sb, 84)
            agcount = u32be(sb, 88)
            sector_size = u16be(sb, 102)
            if not block_size or not dblocks or not agblocks or not agcount or not sector_size:
                raise BackendError("invalid XFS superblock")
            ranges: list[tuple[int, int, int]] = []
            free_total = 0
            ag_details = []
            for agno in range(agcount):
                ag_start = agno * agblocks
                ag_len = min(agblocks, dblocks - ag_start)
                if ag_len <= 0:
                    break
                agf_off = ag_start * block_size + sector_size
                agf = r.read(agf_off, sector_size)
                if agf[:4] != b"XAGF":
                    ranges.append((ag_start, ag_start + ag_len, 2))
                    continue
                freeblks = u32be(agf, 44)
                longest = u32be(agf, 48)
                free_total += min(freeblks, ag_len)
                ag_details.append({"ag": agno, "blocks": ag_len, "free_blocks": freeblks, "longest": longest})
                # AGF gives exact aggregate free counts but not locations without walking bnobt/cntbt.
                ranges.append((ag_start, ag_start + ag_len, 2))
            result = aggregate_ranges(dblocks, cells, block_size, "xfs", ranges,
                                      "allocation-group-summary", {"allocation_groups": ag_details})
            result["free_bytes"] = free_total * block_size
            result["used_bytes"] = max(0, dblocks - free_total) * block_size
            result["unknown_bytes"] = dblocks * block_size
            return result

BACKEND = XfsBackend()
