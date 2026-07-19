# Linux Defragger
# Author: Shannon Smith
# Purpose: Read-only Minix filesystem identification and geometry mapping.

"""Read-only Minix filesystem identification backend."""

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
    "minix",
    "Minix Filesystem",
    ("minix", "minix2", "minix3"),
    CAP_ANALYSE | CAP_MAP,
    "summary",
)

_MAGICS = {
    0x137F: "v1",
    0x138F: "v1-30char",
    0x2468: "v2",
    0x2478: "v2-30char",
    0x4D5A: "v3",
}


class MinixBackend(FilesystemBackend):
    info = INFO

    @staticmethod
    def _read_superblock(reader: Reader) -> tuple[int, str, str]:
        superblock = reader.read(1024, 64)
        for byte_order in ("little", "big"):
            for offset in (16, 24):
                magic = int.from_bytes(superblock[offset:offset + 2], byte_order)
                variant = _MAGICS.get(magic)
                if variant is not None:
                    return magic, variant, byte_order
        raise BackendError("not a recognised Minix filesystem")

    def probe(self, path: str) -> bool:
        try:
            with Reader(path) as reader:
                self._read_superblock(reader)
            return True
        except (OSError, BackendError):
            return False

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            magic, variant, byte_order = self._read_superblock(reader)
            unit_size = 1024
            total_units = max(1, (reader.size + unit_size - 1) // unit_size)
            return aggregate_ranges(
                total_units,
                cells,
                unit_size,
                "minix",
                [(0, total_units, 2)],
                "summary",
                {
                    "magic": hex(magic),
                    "variant": variant,
                    "byte_order": byte_order,
                    "note": (
                        "Minix filesystem detected; zone bitmap location mapping is not yet decoded"
                    ),
                },
            )


BACKEND = MinixBackend()
