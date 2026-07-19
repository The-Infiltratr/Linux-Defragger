#!/usr/bin/python3
"""Standard operation names and common command-line policy."""

from __future__ import annotations

from enum import Enum


class Operation(str, Enum):
    COMPACT = "compact"
    DEFRAG = "defrag"
    GROWTH_DEFRAG = "growth-defrag"
    RECOVER = "recover"

    @classmethod
    def parse(cls, value: str) -> "Operation":
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown operation {value!r}; expected one of: {allowed}") from exc


DISPLAY_NAMES = {
    Operation.COMPACT: "Compact",
    Operation.DEFRAG: "Defragment",
    Operation.GROWTH_DEFRAG: "Growth Defrag",
    Operation.RECOVER: "Recover",
}


def build_standard_arguments(operation: str, live_cells: int) -> list[str]:
    """Return operation-wide tuning flags independent of filesystem choice."""

    selected = Operation.parse(operation)
    common = ["--ram-buffer", "auto", "--workers", "auto"]
    if selected is Operation.DEFRAG:
        return [*common, "--transaction-files", "32", "--live-map-cells", str(live_cells)]
    if selected is Operation.GROWTH_DEFRAG:
        return [
            "--growth-percent",
            "10",
            "--batch-clusters",
            "4096",
            *common,
            "--live-map-cells",
            str(live_cells),
        ]
    if selected is Operation.COMPACT:
        return [*common, "--live-map-cells", str(live_cells)]
    return common
