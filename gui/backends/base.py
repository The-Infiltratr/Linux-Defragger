#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Shared filesystem-backend capability and map-result contracts.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Shared backend ABI, binary readers and map aggregation helpers."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CAP_ANALYSE = 1 << 0
CAP_MAP = 1 << 1
CAP_COMPACT = 1 << 2
CAP_DEFRAG = 1 << 3
CAP_RECOVER = 1 << 4
CAP_LIVE_MAP = 1 << 5
CAP_GROWTH_DEFRAG = 1 << 6


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendInfo:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    capabilities: int
    map_accuracy: str = "exact"


class Reader:
    """Small positional-read wrapper that enforces complete raw-device reads."""
    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        self.size = os.fstat(self.fd).st_size

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise BackendError("negative read")
        data = os.pread(self.fd, length, offset)
        if len(data) != length:
            raise BackendError(f"short read at byte {offset}: wanted {length}, got {len(data)}")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def u16le(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def u32le(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def u64le(b: bytes, o: int) -> int:
    return struct.unpack_from("<Q", b, o)[0]


def u16be(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def u32be(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


def u64be(b: bytes, o: int) -> int:
    return struct.unpack_from(">Q", b, o)[0]


# Convert per-allocation-unit states into the fixed schema consumed by the GUI.
def aggregate_states(states: Iterable[int], total_units: int, cell_count: int, unit_size: int,
                     filesystem: str, accuracy: str = "exact", details: dict | None = None) -> dict:
    """Aggregate 0=free, 1=used, 2=unknown/reserved, 3=bad into map cells."""
    cell_count = max(1, min(cell_count, max(1, total_units)))
    vals = list(states)
    if len(vals) < total_units:
        vals.extend([2] * (total_units - len(vals)))
    cells = []
    free_total = used_total = unknown_total = bad_total = 0
    for i in range(cell_count):
        start = (i * total_units) // cell_count
        end_ex = ((i + 1) * total_units) // cell_count
        if end_ex <= start:
            end_ex = start + 1
        subset = vals[start:end_ex]
        free = subset.count(0)
        used = subset.count(1)
        unknown = subset.count(2)
        bad = subset.count(3)
        free_total += free
        used_total += used
        unknown_total += unknown
        bad_total += bad
        cells.append({
            "start": start,
            "end": end_ex - 1,
            "free": free,
            "used": used,
            "unknown": unknown,
            "bad": bad,
            "fragmented": 0,
            "directory": 0,
        })
    result = {
        "schema": 1,
        "backend": "read-only-domain",
        "filesystem": filesystem,
        "map_accuracy": accuracy,
        "unit_size": unit_size,
        "total_units": total_units,
        "cell_count": cell_count,
        "total_bytes": total_units * unit_size,
        "free_bytes": free_total * unit_size,
        "used_bytes": used_total * unit_size,
        "unknown_bytes": (unknown_total + bad_total) * unit_size,
        "cells": cells,
    }
    if details:
        result["details"] = details
    return result


def aggregate_ranges(total_units: int, cell_count: int, unit_size: int, filesystem: str,
                     ranges: list[tuple[int, int, int]], accuracy: str, details: dict | None = None) -> dict:
    """Range-based aggregator without allocating one byte per unit."""
    cell_count = max(1, min(cell_count, max(1, total_units)))
    cells = []
    free_total = used_total = unknown_total = bad_total = 0
    for i in range(cell_count):
        start = (i * total_units) // cell_count
        end_ex = ((i + 1) * total_units) // cell_count
        counts = [0, 0, 0, 0]
        for rs, re, state in ranges:
            lo = max(start, rs)
            hi = min(end_ex, re)
            if hi > lo:
                counts[state] += hi - lo
        covered = sum(counts)
        if covered < end_ex - start:
            counts[2] += (end_ex - start) - covered
        free_total += counts[0]
        used_total += counts[1]
        unknown_total += counts[2]
        bad_total += counts[3]
        cells.append({"start": start, "end": end_ex - 1, "free": counts[0], "used": counts[1],
                      "unknown": counts[2], "bad": counts[3], "fragmented": 0, "directory": 0})
    result = {"schema": 1, "backend": "read-only-domain", "filesystem": filesystem,
              "map_accuracy": accuracy, "unit_size": unit_size, "total_units": total_units,
              "cell_count": cell_count, "total_bytes": total_units * unit_size,
              "free_bytes": free_total * unit_size, "used_bytes": used_total * unit_size,
              "unknown_bytes": (unknown_total + bad_total) * unit_size, "cells": cells}
    if details:
        result["details"] = details
    return result

def count_set_bits(bitmap: bytes, start_bit: int, end_bit: int) -> int:
    """Count set bits in [start_bit, end_bit) without expanding the bitmap."""
    if end_bit <= start_bit:
        return 0
    first_byte = start_bit >> 3
    last_byte = (end_bit - 1) >> 3
    if first_byte == last_byte:
        mask = ((1 << (end_bit - start_bit)) - 1) << (start_bit & 7)
        return (bitmap[first_byte] & mask).bit_count()
    count = 0
    if start_bit & 7:
        mask = 0xFF << (start_bit & 7)
        count += (bitmap[first_byte] & mask).bit_count()
        first_byte += 1
    full_end = end_bit >> 3
    if full_end > first_byte:
        count += int.from_bytes(bitmap[first_byte:full_end], "little").bit_count()
    if end_bit & 7:
        mask = (1 << (end_bit & 7)) - 1
        count += (bitmap[last_byte] & mask).bit_count()
    return count


def aggregate_bitmap(bitmap: bytes, total_units: int, cell_count: int, unit_size: int,
                     filesystem: str, reserved_prefix: int = 0, details: dict | None = None) -> dict:
    if len(bitmap) * 8 < total_units - reserved_prefix:
        raise BackendError("allocation bitmap is shorter than the filesystem")
    cell_count = max(1, min(cell_count, max(1, total_units)))
    cells = []
    free_total = used_total = unknown_total = 0
    for i in range(cell_count):
        start = (i * total_units) // cell_count
        end_ex = ((i + 1) * total_units) // cell_count
        unknown = max(0, min(end_ex, reserved_prefix) - start)
        data_start = max(start, reserved_prefix)
        data_count = max(0, end_ex - data_start)
        used = count_set_bits(bitmap, data_start - reserved_prefix, end_ex - reserved_prefix) if data_count else 0
        free = data_count - used
        free_total += free
        used_total += used
        unknown_total += unknown
        cells.append({"start": start, "end": end_ex - 1, "free": free, "used": used,
                      "unknown": unknown, "bad": 0, "fragmented": 0, "directory": 0})
    result = {"schema": 1, "backend": "read-only-domain", "filesystem": filesystem,
              "map_accuracy": "exact", "unit_size": unit_size, "total_units": total_units,
              "cell_count": cell_count, "total_bytes": total_units * unit_size,
              "free_bytes": free_total * unit_size, "used_bytes": used_total * unit_size,
              "unknown_bytes": unknown_total * unit_size, "cells": cells}
    if details:
        result["details"] = details
    return result
