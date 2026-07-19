#!/usr/bin/python3
"""Stable capability and operation contracts for filesystem plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntFlag

__all__ = [
    "BackendError",
    "BackendInfo",
    "Capability",
    "FilesystemBackend",
    "OperationSpec",
    "CAP_ANALYSE",
    "CAP_COMPACT",
    "CAP_DEFRAG",
    "CAP_GROWTH_DEFRAG",
    "CAP_LIVE_MAP",
    "CAP_MAP",
    "CAP_RECOVER",
    "operation",
]


class Capability(IntFlag):
    ANALYSE = 1 << 0
    MAP = 1 << 1
    COMPACT = 1 << 2
    DEFRAG = 1 << 3
    RECOVER = 1 << 4
    LIVE_MAP = 1 << 5
    GROWTH_DEFRAG = 1 << 6


CAP_ANALYSE = int(Capability.ANALYSE)
CAP_MAP = int(Capability.MAP)
CAP_COMPACT = int(Capability.COMPACT)
CAP_DEFRAG = int(Capability.DEFRAG)
CAP_RECOVER = int(Capability.RECOVER)
CAP_LIVE_MAP = int(Capability.LIVE_MAP)
CAP_GROWTH_DEFRAG = int(Capability.GROWTH_DEFRAG)

_OPERATION_CAPABILITY = {
    "compact": Capability.COMPACT,
    "defrag": Capability.DEFRAG,
    "growth-defrag": Capability.GROWTH_DEFRAG,
    "recover": Capability.RECOVER,
}

_STANDARD_LABELS = {
    "compact": "Compact",
    "defrag": "Defragment",
    "growth-defrag": "Growth Defrag",
    "recover": "Recover",
}

_STANDARD_DESCRIPTIONS = {
    "compact": "Move supported allocations downward to reduce internal free-space gaps.",
    "defrag": "Rebuild fragmented supported files as contiguous allocations.",
    "growth-defrag": (
        "Rebuild supported files contiguously and leave proportional free growth space after them."
    ),
    "recover": "Complete or roll back an interrupted journalled transaction.",
}


class BackendError(RuntimeError):
    """A filesystem plugin could not safely identify or analyse a volume."""


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Standard mutation declaration supplied by one filesystem plugin."""

    name: str
    worker: str
    label: str = ""
    description: str = ""
    warning: str = ""
    pass_filesystem: bool = False
    unsupported_options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in _OPERATION_CAPABILITY:
            raise ValueError(f"unknown filesystem operation: {self.name}")
        if not self.worker or any(char.isspace() for char in self.worker):
            raise ValueError(f"invalid worker identifier: {self.worker!r}")
        if any(not option.startswith("--") for option in self.unsupported_options):
            raise ValueError("unsupported options must use their long --option form")

    @property
    def capability(self) -> Capability:
        return _OPERATION_CAPABILITY[self.name]

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "worker": self.worker,
            "label": self.label or _STANDARD_LABELS[self.name],
            "description": self.description or _STANDARD_DESCRIPTIONS[self.name],
            "warning": self.warning,
            "pass_filesystem": self.pass_filesystem,
            "unsupported_options": list(self.unsupported_options),
        }


def operation(
    name: str,
    worker: str,
    *,
    warning: str = "",
    description: str = "",
    label: str = "",
    pass_filesystem: bool = False,
    unsupported_options: tuple[str, ...] = (),
) -> OperationSpec:
    """Build a concise operation declaration inside a filesystem plugin."""

    return OperationSpec(
        name=name,
        worker=worker,
        label=label,
        description=description,
        warning=warning,
        pass_filesystem=pass_filesystem,
        unsupported_options=unsupported_options,
    )


@dataclass(frozen=True, slots=True)
class BackendInfo:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    capabilities: int
    map_accuracy: str = "exact"
    operations: tuple[OperationSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or self.id.lower() != self.id:
            raise ValueError("backend id must be a non-empty lowercase identifier")
        if not self.display_name:
            raise ValueError(f"backend {self.id} has no display name")
        aliases = tuple(alias.lower() for alias in self.aliases)
        if len(set(aliases)) != len(aliases):
            raise ValueError(f"backend {self.id} declares duplicate aliases")
        operation_names = [item.name for item in self.operations]
        if len(set(operation_names)) != len(operation_names):
            raise ValueError(f"backend {self.id} declares an operation more than once")
        capabilities = Capability(self.capabilities)
        declared = Capability(0)
        for item in self.operations:
            declared |= item.capability
        mutation_mask = (
            Capability.COMPACT | Capability.DEFRAG | Capability.GROWTH_DEFRAG | Capability.RECOVER
        )
        if capabilities & mutation_mask != declared:
            raise ValueError(
                f"backend {self.id} capability bits and operation declarations disagree: "
                f"capabilities={int(capabilities & mutation_mask)}, operations={int(declared)}"
            )

    def operation(self, name: str) -> OperationSpec | None:
        return next((item for item in self.operations if item.name == name), None)

    def manifest(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "capabilities": self.capabilities,
            "map_accuracy": self.map_accuracy,
            "operations": [item.manifest() for item in self.operations],
        }


class FilesystemBackend(ABC):
    """Required read-only interface for every filesystem plugin."""

    info: BackendInfo

    @abstractmethod
    def probe(self, path: str) -> bool:
        """Return true only when *path* has this filesystem's on-disk signature."""

    @abstractmethod
    def map(self, path: str, cells: int) -> dict:
        """Return the standard allocation-map schema without modifying *path*."""
