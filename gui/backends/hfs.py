#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Read-only classic Macintosh HFS allocation analysis.

"""Classic HFS allocation-bitmap backend."""

from .base import BackendError, BackendInfo, CAP_ANALYSE, CAP_MAP, Reader, aggregate_bitmap, u16be, u32be

INFO = BackendInfo("hfs", "Apple HFS", ("hfs",), CAP_ANALYSE | CAP_MAP, "exact")


class HFSBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            return reader.read(1024, 2) == b"BD"

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            mdb = reader.read(1024, 162)
            if mdb[:2] != b"BD":
                raise BackendError("not a classic HFS volume")
            bitmap_sector = u16be(mdb, 14)
            total_blocks = u16be(mdb, 18)
            block_size = u32be(mdb, 20)
            free_blocks = u16be(mdb, 34)
            file_count = u32be(mdb, 84) if len(mdb) >= 88 else u16be(mdb, 12)
            folder_count = u32be(mdb, 88) if len(mdb) >= 92 else 0
            if not total_blocks or block_size < 512 or block_size & 1:
                raise BackendError("invalid HFS allocation geometry")
            bitmap = bytes(int(f"{value:08b}"[::-1], 2) for value in reader.read(bitmap_sector * 512, (total_blocks + 7) // 8))
            result = aggregate_bitmap(bitmap, total_blocks, cells, block_size, "hfs",
                                      details={"header_free_blocks": free_blocks})
            result["details"].update({"file_count": file_count, "folder_count": folder_count})
            return result


BACKEND = HFSBackend()
