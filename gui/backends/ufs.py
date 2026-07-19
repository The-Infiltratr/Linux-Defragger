# Linux Defragger
# Author: Shannon Smith
# Purpose: Read-only UFS filesystem identification and geometry mapping.

"""Read-only UFS filesystem identification and mapping backend."""

from __future__ import annotations

from .base import (
    BackendError,
    BackendInfo,
    CAP_ANALYSE,
    CAP_MAP,
    FilesystemBackend,
    Reader,
    aggregate_ranges,
)

INFO = BackendInfo(
    "ufs",
    "Solaris/BSD UFS",
    ("ufs", "ufs1", "ufs2", "4.2bsd"),
    CAP_ANALYSE | CAP_MAP,
    "summary",
)

# UFS superblocks occur at filesystem-dependent backup locations. Probe common
# primary offsets and search each superblock window for known UFS1/UFS2 magic.
_CANDIDATES = (8192, 65536, 262144)
_MAGICS = {
    b"\x54\x19\x01\x00": "ufs1-le",
    b"\x00\x01\x19\x54": "ufs1-be",
    b"\x19\x01\x54\x19": "ufs2-le",
    b"\x19\x54\x01\x19": "ufs2-be",
}


class UfsBackend(FilesystemBackend):
    info = INFO

    @staticmethod
    def _find_superblock(reader: Reader) -> tuple[int, int, str]:
        for offset in _CANDIDATES:
            if reader.size and offset >= reader.size:
                continue
            length = min(8192, max(0, reader.size - offset)) if reader.size else 8192
            if length < 512:
                continue
            data = reader.read(offset, length)
            for magic, variant in _MAGICS.items():
                position = data.find(magic)
                if position >= 0:
                    return offset, position, variant
        raise BackendError("not a recognised UFS volume")

    def probe(self, path: str) -> bool:
        try:
            with Reader(path) as reader:
                self._find_superblock(reader)
            return True
        except (OSError, BackendError):
            return False

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            offset, position, variant = self._find_superblock(reader)
            unit_size = 512
            total_units = max(1, (reader.size + unit_size - 1) // unit_size)
            return aggregate_ranges(
                total_units,
                cells,
                unit_size,
                "ufs",
                [(0, total_units, 2)],
                "summary",
                {
                    "variant": variant,
                    "superblock_offset": offset,
                    "magic_offset": offset + position,
                    "note": "UFS detected; cylinder-group allocation locations not yet decoded",
                },
            )


BACKEND = UfsBackend()
