# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Read-only UFS filesystem identification and mapping backend."""

from __future__ import annotations
from .base import *

INFO = BackendInfo("ufs", "Solaris/BSD UFS", ("ufs", "ufs1", "ufs2", "4.2bsd"), CAP_ANALYSE|CAP_MAP, "summary")

# UFS superblocks occur at filesystem-dependent backup locations.  Probe common
# primary offsets and search the superblock window for well-known UFS1/UFS2 magic.
_CANDIDATES = (8192, 65536, 262144)
_MAGICS = {
    b"\x54\x19\x01\x00": "ufs1-le",
    b"\x00\x01\x19\x54": "ufs1-be",
    b"\x19\x01\x54\x19": "ufs2-le",
    b"\x19\x54\x01\x19": "ufs2-be",
}

class UfsBackend:
    info = INFO

    def _find(self, r: Reader):
        for off in _CANDIDATES:
            if r.size and off >= r.size:
                continue
            length = min(8192, max(0, r.size-off)) if r.size else 8192
            if length < 512:
                continue
            data = r.read(off, length)
            for magic, kind in _MAGICS.items():
                pos = data.find(magic)
                if pos >= 0:
                    return off, pos, kind
        raise BackendError("not a recognised UFS volume")

    def probe(self, path: str) -> bool:
        try:
            with Reader(path) as r: self._find(r)
            return True
        except (OSError, BackendError): return False

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            off, pos, kind = self._find(r)
            unit = 512
            total_units = max(1, (r.size + unit - 1)//unit)
            # Exact UFS free placement requires cylinder-group bitmap walking.
            # This backend deliberately exposes the complete address space as unknown.
            return aggregate_ranges(total_units, cells, unit, "ufs", [(0,total_units,2)], "summary", {
                "variant": kind, "superblock_offset": off, "magic_offset": off+pos,
                "note": "UFS detected; cylinder-group allocation locations not yet decoded"
            })

BACKEND = UfsBackend()
