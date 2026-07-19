#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Exact Amiga OFS/FFS allocation analysis and capability declaration.

"""Amiga OFS/FFS backend using the bundled amitools filesystem library."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import *

_HERE = Path(__file__).resolve()
_VENDOR_CANDIDATES = (
    _HERE.parents[1] / "vendor",  # Installed layout: .../linux-defragger/backends/affs.py
    _HERE.parents[2] / "vendor",  # Source layout: .../gui/backends/affs.py
)
_VENDOR = next((candidate for candidate in _VENDOR_CANDIDATES if candidate.is_dir()), None)
if _VENDOR is not None and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.ADFSDir import ADFSDir
from amitools.fs.ADFSFile import ADFSFile
from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice

INFO = BackendInfo(
    "affs",
    "Amiga OFS/FFS",
    ("affs", "amiga", "ofs", "ffs", "dostype"),
    CAP_ANALYSE | CAP_MAP | CAP_COMPACT | CAP_DEFRAG | CAP_RECOVER,
    "exact",
)

_VARIANTS = {
    0: "OFS",
    1: "FFS",
    2: "OFS international",
    3: "FFS international",
    4: "OFS directory cache",
    5: "FFS directory cache",
    6: "OFS long-name",
    7: "FFS long-name",
}


def _open_block_device(path: str, read_only: bool = True):
    """Open an ADF/HDF image or a raw Amiga filesystem partition."""
    suffix = path.lower()
    if suffix.endswith((".adf", ".adz", ".adf.gz", ".hdf", ".hdz", ".hdf.gz", ".rdb", ".rdisk")):
        return BlkDevFactory().open(path, read_only=read_only)
    blk = RawBlockDevice(path, read_only=read_only, block_bytes=512)
    blk.open()
    return blk


def _extent_count(blocks: list[int]) -> int:
    if not blocks:
        return 0
    return 1 + sum(1 for left, right in zip(blocks, blocks[1:]) if right != left + 1)


class AffsBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        try:
            with open(path, "rb", buffering=0) as handle:
                boot = handle.read(4)
            return len(boot) == 4 and boot[:3] == b"DOS" and boot[3] <= 7
        except OSError:
            return False

    def map(self, path: str, cells: int) -> dict:
        blkdev = _open_block_device(path, read_only=True)
        volume = ADFSVolume(blkdev)
        try:
            volume.open()
            total = blkdev.num_blocks
            block_size = blkdev.block_bytes
            states = [2 if index < blkdev.reserved else 1 for index in range(total)]
            for index in range(blkdev.reserved, total):
                states[index] = 0 if volume.bitmap.get_bit(index) else 1

            regular_files = 0
            directories = 1
            fragmented_files = 0
            fragmented_directories = 0
            fragmented_blocks: set[int] = set()
            directory_blocks: set[int] = {volume.root.blk_num}
            directory_blocks.update(block.blk_num for block in volume.bitmap.bitmap_blks)
            directory_blocks.update(block.blk_num for block in volume.bitmap.ext_blks)

            def walk(directory: ADFSDir) -> None:
                nonlocal regular_files, directories, fragmented_files, fragmented_directories
                directory.ensure_entries()
                own_blocks = directory.get_block_nums()
                directory_blocks.update(own_blocks)
                if directory is not volume.root_dir and _extent_count(own_blocks) > 1:
                    fragmented_directories += 1
                    fragmented_blocks.update(own_blocks)
                for node in directory.entries:
                    if isinstance(node, ADFSFile):
                        regular_files += 1
                        data_blocks = list(node.data_blk_nums)
                        if _extent_count(data_blocks) > 1:
                            fragmented_files += 1
                            fragmented_blocks.update(data_blocks)
                    elif isinstance(node, ADFSDir):
                        directories += 1
                        walk(node)

            walk(volume.root_dir)
            result = aggregate_states(states, total, cells, block_size, "affs", "exact", {
                "dostype": f"DOS\\{volume.boot.dos_type & 0xff}",
                "variant": _VARIANTS.get(volume.boot.dos_type & 0xff, "Amiga DOS"),
                "block_size": block_size,
                "volume_name": volume.name.get_unicode() if volume.name else "",
            })
            cell_count = int(result["cell_count"])
            for block in directory_blocks:
                if 0 <= block < total:
                    index = min(cell_count - 1, (block * cell_count) // total)
                    result["cells"][index]["directory"] += 1
            for block in fragmented_blocks:
                if 0 <= block < total:
                    index = min(cell_count - 1, (block * cell_count) // total)
                    result["cells"][index]["fragmented"] += 1
            result.update({
                "regular_files": regular_files,
                "directories": directories,
                "fragmented_files": fragmented_files,
                "fragmented_directories": fragmented_directories,
            })
            return result
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        finally:
            try:
                volume.close()
            except Exception:
                pass
            blkdev.close()


BACKEND = AffsBackend()
