from __future__ import annotations
from .base import *

INFO = BackendInfo("btrfs", "Btrfs", ("btrfs",), CAP_ANALYSE|CAP_MAP, "summary")

class BtrfsBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            return r.read(65536 + 0x40, 8) == b"_BHRfS_M"

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            sb = r.read(65536, 4096)
            if sb[0x40:0x48] != b"_BHRfS_M":
                raise BackendError("not a Btrfs volume")
            total_bytes = u64le(sb, 0x70)
            used_bytes = u64le(sb, 0x78)
            sector_size = u32le(sb, 0x90)
            if sector_size == 0 or total_bytes == 0:
                raise BackendError("invalid Btrfs superblock")
            total_units = (total_bytes + sector_size - 1) // sector_size
            used_units = min(total_units, (used_bytes + sector_size - 1) // sector_size)
            # Btrfs is copy-on-write and physical ownership requires walking chunk,
            # extent and free-space trees. Until that exact walker is implemented,
            # expose truthful aggregate accounting and mark location as unknown.
            ranges = [(0, total_units, 2)]
            result = aggregate_ranges(total_units, cells, sector_size, "btrfs", ranges, "summary",
                                      {"bytes_used": used_bytes, "note": "location unknown; aggregate superblock accounting"})
            result["used_bytes"] = used_units * sector_size
            result["free_bytes"] = max(0, total_bytes - used_bytes)
            result["unknown_bytes"] = total_bytes
            return result

BACKEND = BtrfsBackend()
