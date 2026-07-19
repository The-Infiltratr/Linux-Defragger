#!/usr/bin/env python3
"""Regression tests for shared linear-time allocation-map range aggregation."""

from __future__ import annotations

import random
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))

from backends.contracts import BackendError
from backends.ranges import aggregate_ranges, complement_ranges, merge_ranges, overlay_ranges


def reference(total: int, cells: int, ranges: list[tuple[int, int, int]]) -> list[dict]:
    states = [2] * total
    for start, end, state in ranges:
        for index in range(start, end):
            states[index] = state
    result = []
    for cell in range(cells):
        start = cell * total // cells
        end = (cell + 1) * total // cells
        counts = [states[start:end].count(state) for state in range(4)]
        result.append({"free": counts[0], "used": counts[1], "unknown": counts[2], "bad": counts[3]})
    return result


random.seed(35)
for _case in range(40):
    total = random.randint(50, 500)
    cells = random.randint(1, min(80, total))
    ranges: list[tuple[int, int, int]] = []
    cursor = 0
    while cursor < total:
        cursor += random.randint(0, 4)
        if cursor >= total:
            break
        end = min(total, cursor + random.randint(1, 12))
        ranges.append((cursor, end, random.randint(0, 3)))
        cursor = end
    actual = aggregate_ranges(total, cells, 4096, "test", ranges, "exact")
    expected = reference(total, cells, ranges)
    for cell, wanted in zip(actual["cells"], expected):
        assert {key: cell[key] for key in wanted} == wanted

assert merge_ranges([(5, 7), (0, 2), (2, 5), (10, 12)]) == [(0, 7), (10, 12)]
assert complement_ranges(15, [(0, 7), (10, 12)]) == [(7, 10), (12, 15)]
map_result = aggregate_ranges(20, 4, 1, "test", [(0, 20, 1)], "exact")
assert overlay_ranges(map_result["cells"], [(2, 8), (7, 13)], "fragmented") == 11
assert sum(cell["fragmented"] for cell in map_result["cells"]) == 11
try:
    aggregate_ranges(20, 4, 1, "test", [(0, 10, 1), (9, 12, 0)], "exact")
except BackendError:
    pass
else:
    raise AssertionError("overlapping state ranges were accepted")
print("shared range-helper tests passed")
