#!/usr/bin/env python3
"""Validate the engine/module/filesystem-plugin architecture."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))

import operation_engine
from backends.contracts import Capability, FilesystemBackend
from backends.registry import Registry
from core.operations import Operation
from core.paths import PROGRAMS

registry = Registry()
assert registry.backends
assert len(registry.ids) == len(registry.backends)
assert len(registry.aliases) >= len(registry.backends)

mutation_mask = (
    Capability.COMPACT
    | Capability.DEFRAG
    | Capability.GROWTH_DEFRAG
    | Capability.RECOVER
)
for backend in registry.backends:
    assert isinstance(backend, FilesystemBackend)
    declared = Capability(0)
    for specification in backend.info.operations:
        declared |= specification.capability
        assert specification.worker in PROGRAMS
        assert specification.name in {item.value for item in Operation}
    assert Capability(backend.info.capabilities) & mutation_mask == declared

original_resolver = operation_engine.resolve_program
try:
    operation_engine.resolve_program = lambda worker, anchor=None: f"/worker/{worker}"
    fat = operation_engine.build_worker_command(
        registry,
        "fat32",
        "defrag",
        "/dev/test",
        ["--transaction-files", "32", "--workers", "auto"],
    )
    assert fat == [
        "/worker/fat-native", "defrag", "/dev/test",
        "--transaction-files", "32", "--workers", "auto",
    ]

    ntfs = operation_engine.build_worker_command(
        registry,
        "ntfs",
        "defrag",
        "/dev/test",
        ["--transaction-files", "32", "--workers", "auto"],
    )
    assert ntfs == ["/worker/ntfs", "defrag", "/dev/test", "--workers", "auto"]

    ext4 = operation_engine.build_worker_command(
        registry,
        "ext4",
        "compact",
        "/dev/test",
        ["--workers", "auto"],
    )
    assert ext4 == [
        "/worker/linux-compact", "compact", "/dev/test", "--workers", "auto",
        "--filesystem", "ext4",
    ]
finally:
    operation_engine.resolve_program = original_resolver

helper = (ROOT / "gui" / "privileged_helper.py").read_text()
gui = (ROOT / "gui" / "linux_defragger_gui.py").read_text()
assert 'program == "operation-engine"' in helper
assert "NATIVE_COMPACT_ENGINE" not in helper
assert 'program == "engine"' not in helper
assert "EXFAT_ENGINE" not in helper
assert "NTFS_ENGINE" not in helper
assert "self.operation_engine" in gui
assert "find_native_compact_engine" not in gui
print("filesystem plugin architecture test passed")
