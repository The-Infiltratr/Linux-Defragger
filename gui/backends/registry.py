# Linux Defragger
# Author: Shannon Smith
# Purpose: Validated filesystem-plugin discovery and manifest ordering.

"""Load and validate every built-in filesystem plugin."""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from .base import BackendError, FilesystemBackend


PLUGIN_MODULES = (
    "fat12",
    "fat16",
    "fat32",
    "exfat",
    "ntfs",
    "ext4",
    "btrfs",
    "xfs",
    "swap",
    "ufs",
    "zfs",
    "affs",
    "minix",
    "hfs",
    "hfsplus",
    "apfs",
)


class Registry:
    """Ordered plugin registry with strict ID and alias validation."""

    def __init__(self, module_names: Iterable[str] = PLUGIN_MODULES):
        self.backends: list[FilesystemBackend] = []
        self.aliases: dict[str, FilesystemBackend] = {}
        self.ids: dict[str, FilesystemBackend] = {}
        package = __package__ or "backends"
        for name in module_names:
            module = importlib.import_module(f"{package}.{name}")
            backend = getattr(module, "BACKEND", None)
            if not isinstance(backend, FilesystemBackend):
                raise BackendError(f"plugin {name} does not expose a FilesystemBackend as BACKEND")
            self._register(backend)

    def _register(self, backend: FilesystemBackend) -> None:
        backend_id = backend.info.id.lower()
        if backend_id in self.ids:
            raise BackendError(f"duplicate filesystem plugin id: {backend_id}")
        self.ids[backend_id] = backend
        self.backends.append(backend)
        for alias in (backend_id, *backend.info.aliases):
            key = alias.lower()
            previous = self.aliases.get(key)
            if previous is not None and previous is not backend:
                raise BackendError(
                    f"filesystem alias {key!r} is declared by both "
                    f"{previous.info.id} and {backend.info.id}"
                )
            self.aliases[key] = backend

    def by_fstype(self, fstype: str) -> FilesystemBackend | None:
        return self.aliases.get(fstype.lower())

    def probe(self, path: str) -> FilesystemBackend | None:
        for backend in self.backends:
            try:
                if backend.probe(path):
                    return backend
            except (OSError, BackendError):
                continue
        return None

    def manifest(self) -> list[dict[str, object]]:
        return [backend.info.manifest() for backend in self.backends]
