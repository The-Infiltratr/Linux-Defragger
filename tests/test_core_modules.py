#!/usr/bin/env python3
"""Tests for shared path, mount-state and durable-journal modules."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))

from core.devices import is_mounted
from core.journal import write_json_journal
from core.paths import PROGRAMS, resolve_program

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    image = root / "image.bin"
    image.write_bytes(b"\0" * 4096)
    assert not is_mounted(str(image))

    journal = root / "state" / "operation.json"
    write_json_journal(journal, {"phase": "prepared", "value": 35}, trailing_newline=True)
    assert json.loads(journal.read_text(encoding="utf-8")) == {"phase": "prepared", "value": 35}
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert not list(journal.parent.glob("*.tmp"))

    worker = root / "worker"
    worker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    worker.chmod(0o755)
    spec = PROGRAMS["ntfs"]
    previous = os.environ.get(spec.environment)
    os.environ[spec.environment] = str(worker)
    try:
        assert resolve_program("ntfs") == str(worker)
    finally:
        if previous is None:
            os.environ.pop(spec.environment, None)
        else:
            os.environ[spec.environment] = previous

print("shared core-module tests passed")
