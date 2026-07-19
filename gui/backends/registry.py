# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Filesystem backend registry and capability lookup."""

from __future__ import annotations
import importlib
from dataclasses import asdict
from .base import *

MODULES = ("fat12", "fat16", "fat32", "exfat", "ntfs", "ext4", "btrfs", "xfs", "swap", "ufs", "zfs", "affs", "minix")

class Registry:
    def __init__(self):
        self.backends = []
        self.aliases = {}
        package = __package__ or "backends"
        for name in MODULES:
            module = importlib.import_module(f"{package}.{name}")
            backend = module.BACKEND
            self.backends.append(backend)
            for alias in backend.info.aliases:
                self.aliases[alias.lower()] = backend
            self.aliases[backend.info.id.lower()] = backend

    def by_fstype(self, fstype: str):
        return self.aliases.get(fstype.lower())

    def probe(self, path: str):
        for backend in self.backends:
            try:
                if backend.probe(path):
                    return backend
            except (OSError, BackendError):
                continue
        return None

    def manifest(self):
        result = []
        for backend in self.backends:
            info = backend.info
            result.append({"id": info.id, "display_name": info.display_name,
                           "aliases": list(info.aliases), "capabilities": info.capabilities,
                           "map_accuracy": info.map_accuracy})
        return result
