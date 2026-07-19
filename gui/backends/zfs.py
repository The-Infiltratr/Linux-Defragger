# Linux Defragger
# Author: Shannon Smith
# Purpose: Read-only ZFS member identification and geometry mapping.

"""Read-only ZFS member identification and summary backend."""

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
    "zfs",
    "ZFS/OpenZFS Member",
    ("zfs", "zfs_member"),
    CAP_ANALYSE | CAP_MAP,
    "summary",
)

_UBER_MAGIC_LE = b"\x0c\xb1\xba\x00\x00\x00\x00\x00"
_UBER_MAGIC_BE = _UBER_MAGIC_LE[::-1]
_WINDOW_SIZE = 4 * 1024 * 1024


class ZfsBackend(FilesystemBackend):
    info = INFO

    @staticmethod
    def _find_uberblock(reader: Reader) -> tuple[int, str]:
        if reader.size:
            windows = (
                (0, min(reader.size, _WINDOW_SIZE)),
                (max(0, reader.size - _WINDOW_SIZE), min(reader.size, _WINDOW_SIZE)),
            )
        else:
            windows = ((0, _WINDOW_SIZE),)
        for offset, length in windows:
            if length < 8:
                continue
            data = reader.read(offset, length)
            for magic, byte_order in ((_UBER_MAGIC_LE, "little"), (_UBER_MAGIC_BE, "big")):
                position = data.find(magic)
                if position >= 0:
                    return offset + position, byte_order
        raise BackendError("not a recognised ZFS member")

    def probe(self, path: str) -> bool:
        try:
            with Reader(path) as reader:
                self._find_uberblock(reader)
            return True
        except (OSError, BackendError):
            return False

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            position, byte_order = self._find_uberblock(reader)
            unit_size = 512
            total_units = max(1, (reader.size + unit_size - 1) // unit_size)
            return aggregate_ranges(
                total_units,
                cells,
                unit_size,
                "zfs",
                [(0, total_units, 2)],
                "summary",
                {
                    "uberblock_magic_offset": position,
                    "byte_order": byte_order,
                    "note": (
                        "ZFS member detected; exact allocation requires pool-wide metaslab and "
                        "space-map traversal"
                    ),
                },
            )


BACKEND = ZfsBackend()
