#!/usr/bin/python3
"""Linear-time range, bitmap and map-cell aggregation helpers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from .contracts import BackendError

__all__ = [
    "aggregate_bitmap",
    "aggregate_ranges",
    "aggregate_states",
    "complement_ranges",
    "count_set_bits",
    "merge_ranges",
    "overlay_ranges",
]


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping or adjacent half-open ranges."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if start < 0 or end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def complement_ranges(total_units: int, occupied: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return all gaps inside ``[0, total_units)`` after merging *occupied*."""

    if total_units < 0:
        raise BackendError("negative allocation-unit count")
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merge_ranges(occupied):
        start = min(total_units, max(0, start))
        end = min(total_units, max(0, end))
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
        if cursor >= total_units:
            break
    if cursor < total_units:
        gaps.append((cursor, total_units))
    return gaps


def overlay_ranges(cells: list[dict], ranges: Iterable[tuple[int, int]], field: str) -> int:
    """Overlay merged allocation-unit ranges onto ordered map cells in linear time."""

    merged = merge_ranges(ranges)
    total = sum(end - start for start, end in merged)
    range_index = 0
    for cell in cells:
        start = int(cell["start"])
        end_exclusive = int(cell["end"]) + 1
        while range_index < len(merged) and merged[range_index][1] <= start:
            range_index += 1
        overlap = 0
        check = range_index
        while check < len(merged) and merged[check][0] < end_exclusive:
            overlap += max(
                0,
                min(end_exclusive, merged[check][1]) - max(start, merged[check][0]),
            )
            if merged[check][1] > end_exclusive:
                break
            check += 1
        cell[field] = min(int(cell.get("used", 0)), overlap)
    return total


def _new_map_result(
    *,
    filesystem: str,
    accuracy: str,
    unit_size: int,
    total_units: int,
    cells: list[dict],
    free_total: int,
    used_total: int,
    unknown_total: int,
    bad_total: int = 0,
    details: dict | None = None,
) -> dict:
    result = {
        "schema": 1,
        "backend": "read-only-domain",
        "filesystem": filesystem,
        "map_accuracy": accuracy,
        "unit_size": unit_size,
        "total_units": total_units,
        "cell_count": len(cells),
        "total_bytes": total_units * unit_size,
        "free_bytes": free_total * unit_size,
        "used_bytes": used_total * unit_size,
        "unknown_bytes": (unknown_total + bad_total) * unit_size,
        "cells": cells,
    }
    if details:
        result["details"] = details
    return result


def _cell_bounds(total_units: int, cell_count: int) -> Iterator[tuple[int, int]]:
    for index in range(cell_count):
        start = (index * total_units) // cell_count
        end_exclusive = ((index + 1) * total_units) // cell_count
        yield start, max(start + 1, end_exclusive)


def aggregate_states(
    states: Iterable[int],
    total_units: int,
    cell_count: int,
    unit_size: int,
    filesystem: str,
    accuracy: str = "exact",
    details: dict | None = None,
) -> dict:
    """Aggregate a sequential state stream without copying the complete map."""

    cell_count = max(1, min(cell_count, max(1, total_units)))
    values = iter(states)
    consumed = 0
    exhausted = False
    cells: list[dict] = []
    totals = [0, 0, 0, 0]
    for start, end_exclusive in _cell_bounds(total_units, cell_count):
        counts = [0, 0, 0, 0]
        # Plugins provide states in physical order. Missing trailing states are
        # conservatively unknown; surplus states are intentionally ignored.
        while consumed < end_exclusive:
            if exhausted:
                state = 2
            else:
                try:
                    state = next(values)
                except StopIteration:
                    exhausted = True
                    state = 2
            counts[state if 0 <= state <= 3 else 2] += 1
            consumed += 1
        for state, count in enumerate(counts):
            totals[state] += count
        cells.append(
            {
                "start": start,
                "end": end_exclusive - 1,
                "free": counts[0],
                "used": counts[1],
                "unknown": counts[2],
                "bad": counts[3],
                "fragmented": 0,
                "directory": 0,
            }
        )
    return _new_map_result(
        filesystem=filesystem,
        accuracy=accuracy,
        unit_size=unit_size,
        total_units=total_units,
        cells=cells,
        free_total=totals[0],
        used_total=totals[1],
        unknown_total=totals[2],
        bad_total=totals[3],
        details=details,
    )

def _normalise_state_ranges(
    ranges: Sequence[tuple[int, int, int]], total_units: int
) -> list[tuple[int, int, int]]:
    normalised: list[tuple[int, int, int]] = []
    for start, end, state in sorted(ranges, key=lambda item: (item[0], item[1], item[2])):
        start = max(0, min(total_units, int(start)))
        end = max(0, min(total_units, int(end)))
        if end <= start:
            continue
        if state not in (0, 1, 2, 3):
            raise BackendError(f"invalid allocation state {state}")
        if normalised and start < normalised[-1][1]:
            raise BackendError("overlapping allocation-state ranges")
        if normalised and start == normalised[-1][1] and state == normalised[-1][2]:
            previous = normalised[-1]
            normalised[-1] = (previous[0], end, state)
        else:
            normalised.append((start, end, state))
    return normalised


def aggregate_ranges(
    total_units: int,
    cell_count: int,
    unit_size: int,
    filesystem: str,
    ranges: list[tuple[int, int, int]],
    accuracy: str,
    details: dict | None = None,
) -> dict:
    """Aggregate non-overlapping state ranges in O(cells + ranges) time."""

    cell_count = max(1, min(cell_count, max(1, total_units)))
    ordered = _normalise_state_ranges(ranges, total_units)
    cells: list[dict] = []
    totals = [0, 0, 0, 0]
    range_index = 0
    for start, end_exclusive in _cell_bounds(total_units, cell_count):
        counts = [0, 0, 0, 0]
        cursor = start
        while range_index < len(ordered) and ordered[range_index][1] <= start:
            range_index += 1
        check = range_index
        while check < len(ordered) and ordered[check][0] < end_exclusive:
            range_start, range_end, state = ordered[check]
            overlap_start = max(start, range_start)
            overlap_end = min(end_exclusive, range_end)
            if overlap_start > cursor:
                counts[2] += overlap_start - cursor
            if overlap_end > overlap_start:
                counts[state] += overlap_end - overlap_start
                cursor = max(cursor, overlap_end)
            if range_end > end_exclusive:
                break
            check += 1
        if cursor < end_exclusive:
            counts[2] += end_exclusive - cursor
        for state, count in enumerate(counts):
            totals[state] += count
        cells.append(
            {
                "start": start,
                "end": end_exclusive - 1,
                "free": counts[0],
                "used": counts[1],
                "unknown": counts[2],
                "bad": counts[3],
                "fragmented": 0,
                "directory": 0,
            }
        )
    return _new_map_result(
        filesystem=filesystem,
        accuracy=accuracy,
        unit_size=unit_size,
        total_units=total_units,
        cells=cells,
        free_total=totals[0],
        used_total=totals[1],
        unknown_total=totals[2],
        bad_total=totals[3],
        details=details,
    )


def count_set_bits(bitmap: bytes, start_bit: int, end_bit: int) -> int:
    """Count set bits in ``[start_bit, end_bit)`` without expanding the bitmap."""

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


def aggregate_bitmap(
    bitmap: bytes,
    total_units: int,
    cell_count: int,
    unit_size: int,
    filesystem: str,
    reserved_prefix: int = 0,
    details: dict | None = None,
) -> dict:
    if len(bitmap) * 8 < total_units - reserved_prefix:
        raise BackendError("allocation bitmap is shorter than the filesystem")
    cell_count = max(1, min(cell_count, max(1, total_units)))
    cells: list[dict] = []
    free_total = used_total = unknown_total = 0
    for start, end_exclusive in _cell_bounds(total_units, cell_count):
        unknown = max(0, min(end_exclusive, reserved_prefix) - start)
        data_start = max(start, reserved_prefix)
        data_count = max(0, end_exclusive - data_start)
        used = (
            count_set_bits(bitmap, data_start - reserved_prefix, end_exclusive - reserved_prefix)
            if data_count
            else 0
        )
        free = data_count - used
        free_total += free
        used_total += used
        unknown_total += unknown
        cells.append(
            {
                "start": start,
                "end": end_exclusive - 1,
                "free": free,
                "used": used,
                "unknown": unknown,
                "bad": 0,
                "fragmented": 0,
                "directory": 0,
            }
        )
    return _new_map_result(
        filesystem=filesystem,
        accuracy="exact",
        unit_size=unit_size,
        total_units=total_units,
        cells=cells,
        free_total=free_total,
        used_total=used_total,
        unknown_total=unknown_total,
        details=details,
    )
