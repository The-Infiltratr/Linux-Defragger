#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Classic Macintosh HFS analysis, compaction and defragmentation.

"""Classic HFS allocation mapping and mutation-capability backend."""

import json
import os
import subprocess
from pathlib import Path

from .base import (BackendError, BackendInfo, CAP_ANALYSE, CAP_MAP, CAP_COMPACT,
                   CAP_DEFRAG, CAP_RECOVER, FilesystemBackend, Reader, aggregate_bitmap, operation, u16be, u32be)

INFO = BackendInfo(
    "hfs", "Apple HFS", ("hfs",),
    CAP_ANALYSE | CAP_MAP | CAP_COMPACT | CAP_DEFRAG | CAP_RECOVER,
    "exact",
    (
        operation("compact", "apple"),
        operation("defrag", "apple"),
        operation("recover", "apple"),
    ),
)


class HFSBackend(FilesystemBackend):
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
            candidates = (
                Path(__file__).resolve().parents[1] / "hfs_engine",
                Path("/usr/lib/linux-defragger/hfs_engine"),
            )
            scanner = next((item for item in candidates if item.is_file() and os.access(item, os.X_OK)), None)
            if scanner is not None:
                try:
                    completed = subprocess.run([str(scanner), "scan-json", path], check=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, env={**os.environ, "LC_ALL": "C"})
                    summary = json.loads(completed.stdout)
                    result.update({
                        "regular_files": int(summary.get("files", file_count)),
                        "directories": int(summary.get("directories", folder_count + 1)),
                        "fragmented_files": int(summary.get("fragmented_files", 0)),
                        "fragmented_directories": int(summary.get("fragmented_directories", 0)),
                    })
                    fragmented = set()
                    for start, count in summary.get("fragmented_extents", []):
                        fragmented.update(range(int(start), int(start) + int(count)))
                    for cell in result["cells"]:
                        start, end = cell["start"], cell["end"]
                        cell["fragmented"] = sum(1 for block in fragmented if start <= block <= end)
                except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                    pass
            return result


BACKEND = HFSBackend()
