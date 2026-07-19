#!/usr/bin/python3
"""Mount-state helpers shared by filesystem mutation workers."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_MOUNTINFO = Path("/proc/self/mountinfo")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _decode_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _mount_records() -> list[tuple[str, str]]:
    """Return ``(major:minor, source)`` pairs from the current mount namespace."""

    records: list[tuple[str, str]] = []
    try:
        lines = _MOUNTINFO.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for line in lines:
        fields = line.split()
        separator = fields.index("-") if "-" in fields else -1
        if separator < 0 or len(fields) <= separator + 2 or len(fields) <= 2:
            continue
        records.append((fields[2], _decode_mount_field(fields[separator + 2])))
    return records


def is_mounted(path: str) -> bool:
    """Return whether a block device or image path is mounted in this namespace."""

    real = os.path.realpath(path)
    try:
        target = os.stat(real)
    except OSError:
        return False
    device_id = (
        f"{os.major(target.st_rdev)}:{os.minor(target.st_rdev)}"
        if stat.S_ISBLK(target.st_mode)
        else None
    )
    for mounted_id, source in _mount_records():
        if device_id is not None and mounted_id == device_id:
            return True
        try:
            if source and os.path.realpath(source) == real:
                return True
        except OSError:
            continue
    return False


def require_unmounted(path: str, *, block_device: bool = False) -> None:
    """Validate a mutation target before a worker opens it for writing."""

    try:
        target = os.stat(path)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect target {path}: {exc}") from exc
    if block_device and not stat.S_ISBLK(target.st_mode):
        raise RuntimeError("this operation requires a real block-device partition")
    if is_mounted(path):
        raise RuntimeError(f"{path} is mounted; this operation requires an unmounted volume")
