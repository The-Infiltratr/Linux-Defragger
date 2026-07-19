#!/usr/bin/python3
"""Regression checks for live NTFS allocation-map movement events."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
import ntfs_engine as n


def test_split_run_event_sequence() -> None:
    move = n.ExtentMove(
        source_runs=(n.Run(100, 3), n.Run(200, 5)),
        destination_runs=(n.Run(10, 4), n.Run(30, 4)),
        new_runs=(n.Run(10, 4), n.Run(30, 4)),
    )
    assert list(n._iter_physical_move_slices(move.source_runs, move.destination_runs)) == [
        (100, 10, 3),
        (200, 13, 1),
        (201, 30, 4),
    ]

    output = io.StringIO()
    with redirect_stdout(output):
        n._emit_live_move(move, 4096, 8, 1000, pass_number=1)
    events = [
        json.loads(line.split(" ", 1)[1])
        for line in output.getvalue().splitlines()
        if line.startswith("@@LIVE_RANGE ")
    ]
    assert len(events) == 3
    assert events[0] == {
        "source_start_byte": 100 * 4096,
        "destination_start_byte": 10 * 4096,
        "length_bytes": 3 * 4096,
        "moved_total_bytes": 8 * 4096,
        "pass": 1,
    }
    assert events[-1]["source_start_byte"] == 201 * 4096
    assert events[-1]["destination_start_byte"] == 30 * 4096


def test_disabled_live_map_emits_nothing() -> None:
    move = n.ExtentMove((n.Run(9, 1),), (n.Run(2, 1),), (n.Run(2, 1),))
    output = io.StringIO()
    with redirect_stdout(output):
        n._emit_live_move(move, 4096, 1, 0)
    assert output.getvalue() == ""


if __name__ == "__main__":
    test_split_run_event_sequence()
    test_disabled_live_map_emits_nothing()
    print("NTFS live allocation-map regression checks passed")
