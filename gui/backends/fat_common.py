# Linux Defragger
# Author: Shannon Smith
# Purpose: Shared FAT12/16/32 plugin declaration and signature probe.

"""Standard FAT plugin shared by the three on-disk FAT widths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from core.paths import resolve_program

from .base import (
    BackendError,
    BackendInfo,
    CAP_ANALYSE,
    CAP_COMPACT,
    CAP_DEFRAG,
    CAP_GROWTH_DEFRAG,
    CAP_LIVE_MAP,
    CAP_MAP,
    CAP_RECOVER,
    FilesystemBackend,
    Reader,
    operation,
    u16le,
    u32le,
)


class FatBackend(FilesystemBackend):
    def __init__(self, fat_bits: int):
        self.fat_bits = fat_bits
        self.info = BackendInfo(
            id=f"fat{fat_bits}",
            display_name=f"FAT{fat_bits}",
            aliases=(f"fat{fat_bits}", "vfat" if fat_bits == 32 else f"msdos{fat_bits}"),
            capabilities=(
                CAP_ANALYSE
                | CAP_MAP
                | CAP_COMPACT
                | CAP_DEFRAG
                | CAP_RECOVER
                | CAP_LIVE_MAP
                | CAP_GROWTH_DEFRAG
            ),
            map_accuracy="exact",
            operations=(
                operation("compact", "fat-native"),
                operation("defrag", "fat-native"),
                operation("growth-defrag", "fat-native"),
                operation("recover", "fat-native"),
            ),
        )

    def probe(self, path: str) -> bool:
        with Reader(path) as reader:
            boot_sector = reader.read(0, 512)
        bytes_per_sector = u16le(boot_sector, 11)
        sectors_per_cluster = boot_sector[13]
        reserved = u16le(boot_sector, 14)
        fats = boot_sector[16]
        root_entries = u16le(boot_sector, 17)
        total_sectors = u16le(boot_sector, 19) or u32le(boot_sector, 32)
        fat_sectors = u16le(boot_sector, 22) or u32le(boot_sector, 36)
        if not (
            bytes_per_sector
            and sectors_per_cluster
            and reserved
            and fats
            and total_sectors
            and fat_sectors
        ):
            return False
        root_sectors = ((root_entries * 32) + bytes_per_sector - 1) // bytes_per_sector
        data_sectors = total_sectors - (reserved + fats * fat_sectors + root_sectors)
        clusters = data_sectors // sectors_per_cluster
        detected = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
        return detected == self.fat_bits

    def map(self, path: str, cells: int) -> dict:
        """Delegate the mature FAT scanner through the standard plugin method."""

        anchor = Path(__file__).resolve().parents[1] / "core"
        worker = resolve_program("fat-native", anchor=anchor)
        completed = subprocess.run(
            [worker, "map", path, "--cells", str(max(1, cells))],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise BackendError(detail or "native FAT mapper failed")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"native FAT mapper returned invalid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise BackendError("native FAT mapper returned a non-object result")
        return result
