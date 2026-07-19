#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

import json
import sys

path = sys.argv[1]
expected_cells = int(sys.argv[2])
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)

assert data["program"] == "linux-defragger-engine"
assert data["version"] == "1.8.0-28"
assert data["filesystem"] == "FAT32"
assert data["cell_count"] == expected_cells
assert len(data["cells"]) == expected_cells
assert data["fragmented_files"] >= 1
assert data["data_clusters"] == data["free_clusters"] + data["used_clusters"]

covered = 0
free = 0
used = 0
fragmented = 0
last_end = 1
for cell in data["cells"]:
    assert cell["start"] == last_end + 1
    assert cell["end"] >= cell["start"]
    count = cell["end"] - cell["start"] + 1
    assert cell["free"] + cell["used"] == count
    assert 0 <= cell["fragmented"] <= cell["used"]
    assert 0 <= cell["directory"] <= cell["used"]
    assert 0 <= cell["bad"] <= cell["used"]
    covered += count
    free += cell["free"]
    used += cell["used"]
    fragmented += cell["fragmented"]
    last_end = cell["end"]

assert covered == data["data_clusters"]
assert free == data["free_clusters"]
assert used == data["used_clusters"]
assert fragmented > 0
print("map JSON verified")
