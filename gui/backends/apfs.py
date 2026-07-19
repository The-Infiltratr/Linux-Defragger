#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Conservative read-only APFS container identification and geometry map.

"""APFS container summary backend.

APFS allocation ownership is checkpointed through spaceman objects and can be
shared by multiple volumes. Until those trees are decoded, the backend marks
physical blocks unknown rather than inventing free-space locations.
"""

from __future__ import annotations

from .base import (
    BackendError,
    BackendInfo,
    CAP_ANALYSE,
    CAP_MAP,
    FilesystemBackend,
    Reader,
    aggregate_ranges,
    u32le,
    u64le,
)

INFO = BackendInfo(
    "apfs",
    "Apple APFS",
    ("apfs",),
    CAP_ANALYSE | CAP_MAP,
    "summary",
)


class APFSBackend(FilesystemBackend):
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            return reader.read(32, 4) == b"NXSB"

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            superblock = reader.read(0, 4096)
            if superblock[32:36] != b"NXSB":
                raise BackendError("not an APFS container")
            block_size = u32le(superblock, 36)
            block_count = u64le(superblock, 40)
            if block_size < 4096 or block_size & (block_size - 1) or not block_count:
                raise BackendError("invalid APFS container geometry")
            # Block zero contains the current NX superblock. Remaining blocks
            # are unknown until the spaceman free-space trees are decoded.
            return aggregate_ranges(
                block_count,
                cells,
                block_size,
                "apfs",
                [(0, 1, 1), (1, block_count, 2)],
                "summary",
                details={
                    "container_uuid": superblock[72:88].hex(),
                    "note": "APFS spaceman allocation map not yet decoded",
                },
            )


BACKEND = APFSBackend()
