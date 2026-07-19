#!/usr/bin/python3
"""Regression tests for swap header, runtime accounting and unknown map display."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui"
sys.path.insert(0, str(GUI))
from backends import swap


def make_swap(path: Path, pages: int = 64, page_size: int = 4096) -> None:
    with path.open("wb") as image:
        image.truncate(pages * page_size)
    header = bytearray(page_size)
    struct.pack_into("<I", header, 1024, 1)
    struct.pack_into("<I", header, 1028, pages - 1)
    struct.pack_into("<I", header, 1032, 1)
    header[1052:1056] = b"TEST"
    struct.pack_into("<I", header, 1536, 7)
    header[-10:] = b"SWAPSPACE2"
    with path.open("r+b") as image:
        image.write(header)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        image = temp / "swap.img"
        make_swap(image)

        old_proc = swap._PROC_SWAPS
        try:
            inactive_proc = temp / "inactive-swaps"
            inactive_proc.write_text("Filename\tType\tSize\tUsed\tPriority\n")
            swap._PROC_SWAPS = str(inactive_proc)
            inactive = swap.BACKEND.map(str(image), 32)
            assert inactive["used_bytes"] == 0
            assert inactive["free_bytes"] == (64 - 2) * 4096  # header + one bad page
            assert inactive["details"]["active"] is False
            assert any(cell["free"] for cell in inactive["cells"])
            assert any(cell["bad"] for cell in inactive["cells"])

            active_proc = temp / "active-swaps"
            active_proc.write_text(
                "Filename\tType\tSize\tUsed\tPriority\n"
                f"{image}\tpartition\t252\t64\t-2\n"
            )
            swap._PROC_SWAPS = str(active_proc)
            active = swap.BACKEND.map(str(image), 32)
            assert active["total_bytes"] == 252 * 1024
            assert active["used_bytes"] == 64 * 1024
            assert active["free_bytes"] == 188 * 1024
            assert active["unknown_bytes"] == 252 * 1024
            assert active["details"]["active"] is True
            assert active["details"]["used_pages"] == 16
            assert any(cell["unknown"] for cell in active["cells"])

            output = subprocess.check_output(
                [sys.executable, str(GUI / "allocation_mapper.py"), str(image), "--fstype", "swap", "--cells", "16"],
                text=True,
                env={**os.environ, "PYTHONPATH": str(GUI)},
            )
            parsed = json.loads(output)
            assert parsed["filesystem"] == "swap"
        finally:
            swap._PROC_SWAPS = old_proc
    print("swap backend tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
