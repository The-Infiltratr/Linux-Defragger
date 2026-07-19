#!/usr/bin/python3
"""Single executable-path registry shared by the GUI and operation dispatcher."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProgramPath:
    id: str
    environment: str
    installed: str
    source_relative: str | None = None


PROGRAMS: dict[str, ProgramPath] = {
    "fat-native": ProgramPath(
        "fat-native", "LINUX_DEFRAGGER_ENGINE", "/usr/bin/linux-defragger-engine", "../../build/linux-defragger-engine"
    ),
    "mapper": ProgramPath(
        "mapper", "LINUX_DEFRAGGER_MAPPER", "/usr/lib/linux-defragger/allocation_mapper.py", "../allocation_mapper.py"
    ),
    "operation-engine": ProgramPath(
        "operation-engine", "LINUX_DEFRAGGER_OPERATION_ENGINE", "/usr/lib/linux-defragger/operation_engine.py", "../operation_engine.py"
    ),
    "helper": ProgramPath(
        "helper", "LINUX_DEFRAGGER_HELPER", "/usr/lib/linux-defragger/privileged_helper.py", "../privileged_helper.py"
    ),
    "exfat": ProgramPath(
        "exfat", "LINUX_DEFRAGGER_EXFAT_ENGINE", "/usr/lib/linux-defragger/exfat_engine.py", "../exfat_engine.py"
    ),
    "affs": ProgramPath(
        "affs", "LINUX_DEFRAGGER_AFFS_ENGINE", "/usr/lib/linux-defragger/affs_engine.py", "../affs_engine.py"
    ),
    "apple": ProgramPath(
        "apple", "LINUX_DEFRAGGER_APPLE_ENGINE", "/usr/lib/linux-defragger/apple_engine.py", "../apple_engine.py"
    ),
    "ntfs": ProgramPath(
        "ntfs", "LINUX_DEFRAGGER_NTFS_ENGINE", "/usr/lib/linux-defragger/ntfs_engine.py", "../ntfs_engine.py"
    ),
    "linux-compact": ProgramPath(
        "linux-compact",
        "LINUX_DEFRAGGER_NATIVE_COMPACT_ENGINE",
        "/usr/lib/linux-defragger/native_compact_engine.py",
        "../native_compact_engine.py",
    ),
}


def resolve_program(program_id: str, *, anchor: Path | None = None) -> str:
    """Resolve an executable from an override, source tree or installed path."""

    try:
        spec = PROGRAMS[program_id]
    except KeyError as exc:
        raise FileNotFoundError(f"unknown Linux Defragger program: {program_id}") from exc

    override = os.environ.get(spec.environment)
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    if anchor is not None and spec.source_relative:
        candidates.append((anchor / spec.source_relative).resolve())
    candidates.append(Path(spec.installed))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    rendered = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"could not locate {program_id}; checked: {rendered}")
