#!/usr/bin/python3
"""Durable external-journal primitives shared by mutation workers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def fsync_directory(path: str | Path) -> None:
    """Flush directory-entry changes to stable storage."""

    directory = os.open(
        str(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_json_journal(
    path: str | Path,
    record: Mapping[str, Any],
    *,
    mode: int = 0o600,
    trailing_newline: bool = False,
) -> None:
    """Atomically replace a private JSON journal and flush its directory."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"), sort_keys=True)
            if trailing_newline:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fsync_directory(target.parent)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
