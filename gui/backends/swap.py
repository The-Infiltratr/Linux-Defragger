from __future__ import annotations
from .base import *

INFO = BackendInfo("swap", "Linux/Solaris Swap", ("swap", "swapspace", "linux-swap", "solaris-swap"), CAP_ANALYSE|CAP_MAP, "summary")

_PAGE_SIZES = (4096, 8192, 16384, 32768, 65536)
_SIGS = (b"SWAPSPACE2", b"SWAP-SPACE")

class SwapBackend:
    info = INFO

    def _header(self, r: Reader):
        for page in _PAGE_SIZES:
            if r.size and page > r.size:
                continue
            try:
                sig = r.read(page - 10, 10)
            except BackendError:
                continue
            if sig in _SIGS:
                hdr = r.read(0, page)
                version = u32le(hdr, 1024) if page >= 1028 else 0
                last_page = u32le(hdr, 1028) if page >= 1032 else 0
                badpages = u32le(hdr, 1032) if page >= 1036 else 0
                label = hdr[1052:1068].split(b"\0",1)[0].decode("utf-8", "replace") if page >= 1068 else ""
                return page, sig.decode("ascii"), version, last_page, badpages, label
        raise BackendError("not a recognised swap area")

    def probe(self, path: str) -> bool:
        try:
            with Reader(path) as r:
                self._header(r)
            return True
        except (OSError, BackendError):
            return False

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            page, sig, version, last_page, badpages, label = self._header(r)
            total_bytes = r.size or ((last_page + 1) * page if last_page else page)
            total_units = max(1, (total_bytes + page - 1) // page)
            # Swap usage is kernel runtime state, not a persistent on-disk allocation map.
            # Mark the header as reserved and all slots as unknown rather than inventing usage.
            ranges = [(0, 1, 2), (1, total_units, 2)]
            return aggregate_ranges(total_units, cells, page, "swap", ranges, "summary", {
                "signature": sig, "version": version, "last_page": last_page,
                "bad_pages": badpages, "label": label,
                "note": "swap slot occupancy is runtime kernel state; physical locations are intentionally unknown"
            })

BACKEND = SwapBackend()
