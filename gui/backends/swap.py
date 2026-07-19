# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Read-only Linux and Solaris swap metadata and runtime-usage backend."""

from __future__ import annotations

import os
import re

from .base import *

INFO = BackendInfo(
    "swap",
    "Linux/Solaris Swap",
    ("swap", "swapspace", "linux-swap", "solaris-swap"),
    CAP_ANALYSE | CAP_MAP,
    "summary",
)

_PAGE_SIZES = (4096, 8192, 16384, 32768, 65536)
_SIGS = (b"SWAPSPACE2", b"SWAP-SPACE")
_PROC_SWAPS = "/proc/swaps"
_BADPAGES_OFFSET = 1536


def _decode_proc_path(value: str) -> str:
    """Decode the octal escapes used by procfs for whitespace in paths."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _runtime_usage(path: str) -> tuple[int, int] | None:
    """Return active swap total/used bytes from the kernel, when available."""
    wanted = os.path.realpath(path)
    try:
        with open(_PROC_SWAPS, "r", encoding="utf-8", errors="replace") as swaps:
            next(swaps, None)
            for line in swaps:
                fields = line.split()
                if len(fields) < 5:
                    continue
                candidate = os.path.realpath(_decode_proc_path(fields[0]))
                if candidate != wanted:
                    continue
                try:
                    total = max(0, int(fields[2])) * 1024
                    used = max(0, int(fields[3])) * 1024
                except ValueError:
                    continue
                return total, min(used, total)
    except OSError:
        pass
    return None


def _state_ranges(total_units: int, bad_pages: list[int], active: bool) -> list[tuple[int, int, int]]:
    """Build non-overlapping ranges: header reserved, bad pages, and slots."""
    if total_units <= 0:
        return [(0, 1, 2)]
    ranges: list[tuple[int, int, int]] = [(0, 1, 2)]
    cursor = 1
    slot_state = 2 if active else 0
    for page_no in sorted({p for p in bad_pages if 1 <= p < total_units}):
        if page_no > cursor:
            ranges.append((cursor, page_no, slot_state))
        ranges.append((page_no, page_no + 1, 3))
        cursor = page_no + 1
    if cursor < total_units:
        ranges.append((cursor, total_units, slot_state))
    return ranges


class SwapBackend:
    info = INFO

    def _header(self, reader: Reader):
        for page_size in _PAGE_SIZES:
            if reader.size and page_size > reader.size:
                continue
            try:
                signature = reader.read(page_size - 10, 10)
            except BackendError:
                continue
            if signature not in _SIGS:
                continue
            header = reader.read(0, page_size)
            version = u32le(header, 1024) if page_size >= 1028 else 0
            last_page = u32le(header, 1028) if page_size >= 1032 else 0
            badpage_count = u32le(header, 1032) if page_size >= 1036 else 0
            label = (
                header[1052:1068].split(b"\0", 1)[0].decode("utf-8", "replace")
                if page_size >= 1068
                else ""
            )
            max_badpages = max(0, (page_size - _BADPAGES_OFFSET) // 4)
            badpage_count = min(badpage_count, max_badpages)
            bad_pages = [
                u32le(header, _BADPAGES_OFFSET + index * 4)
                for index in range(badpage_count)
            ]
            return (
                page_size,
                signature.decode("ascii"),
                version,
                last_page,
                bad_pages,
                label,
            )
        raise BackendError("not a recognised swap area")

    def probe(self, path: str) -> bool:
        try:
            with Reader(path) as reader:
                self._header(reader)
            return True
        except (OSError, BackendError):
            return False

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as reader:
            page_size, signature, version, last_page, bad_pages, label = self._header(reader)
            physical_bytes = reader.size or ((last_page + 1) * page_size if last_page else page_size)
            total_units = max(1, (physical_bytes + page_size - 1) // page_size)
            runtime = _runtime_usage(path)
            active = runtime is not None
            ranges = _state_ranges(total_units, bad_pages, active)
            result = aggregate_ranges(
                total_units,
                cells,
                page_size,
                "swap",
                ranges,
                "summary",
                {
                    "signature": signature,
                    "version": version,
                    "last_page": last_page,
                    "bad_pages": len(bad_pages),
                    "label": label,
                    "active": active,
                },
            )

            if active:
                runtime_total, runtime_used = runtime
                result["total_bytes"] = runtime_total
                result["used_bytes"] = runtime_used
                result["free_bytes"] = max(0, runtime_total - runtime_used)
                result["unknown_bytes"] = runtime_total
                result["details"].update(
                    {
                        "usage_source": _PROC_SWAPS,
                        "used_pages": (runtime_used + page_size - 1) // page_size,
                        "free_pages": max(0, runtime_total - runtime_used) // page_size,
                        "note": (
                            "aggregate usage comes from the running kernel; Linux does not expose "
                            "the physical locations of occupied swap slots"
                        ),
                    }
                )
            else:
                bad_bytes = len({p for p in bad_pages if 1 <= p < total_units}) * page_size
                result["used_bytes"] = 0
                result["free_bytes"] = max(0, physical_bytes - page_size - bad_bytes)
                result["unknown_bytes"] = min(page_size + bad_bytes, physical_bytes)
                result["details"].update(
                    {
                        "usage_source": "inactive swap area",
                        "used_pages": 0,
                        "free_pages": max(0, total_units - 1 - len(bad_pages)),
                        "note": "inactive swap has no occupied slots; the header and bad pages remain reserved",
                    }
                )
            return result


BACKEND = SwapBackend()
