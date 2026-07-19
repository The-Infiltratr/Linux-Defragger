#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Validate native NTFS compaction on a genuinely formatted image.

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
import ntfs_engine

IMAGE_SIZE = 768 * 1024 * 1024
FILLER_SIZE = 400 * 1024 * 1024
PAYLOAD_SIZE = 8 * 1024 * 1024


def run(command: list[str], output: Path | None = None) -> str:
    if output is None:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=300, check=False, text=True)
        diagnostic = result.stdout or ""
    else:
        with output.open("wb") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE,
                                    timeout=300, check=False)
        diagnostic = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(f"command failed ({result.returncode}): {' '.join(command)}\n{diagnostic}")
    return diagnostic


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main() -> None:
    tools = {name: shutil.which(name) for name in ("mkntfs", "ntfscp", "ntfscat", "ntfsresize")}
    if not all(tools.values()):
        print("Real native NTFS validation skipped: independent NTFS test utilities are unavailable")
        return
    with tempfile.TemporaryDirectory(prefix="linux-defragger-native-real-ntfs-") as directory:
        tmp = Path(directory)
        image = tmp / "volume.img"
        filler = tmp / "filler.bin"
        tiny = tmp / "tiny.bin"
        payload = tmp / "payload.bin"
        extracted = tmp / "payload-after.bin"
        journal = tmp / "native.journal"
        with image.open("wb") as stream:
            stream.truncate(IMAGE_SIZE)
        run([tools["mkntfs"], "-F", "-Q", "-L", "LINUX_DEFRAG_NATIVE", str(image)])
        with filler.open("wb") as stream:
            stream.truncate(FILLER_SIZE)
        block = bytes((index * 37 + 11) & 0xFF for index in range(1024 * 1024))
        with payload.open("wb") as stream:
            for _ in range(PAYLOAD_SIZE // len(block)):
                stream.write(block)
        tiny.write_bytes(b"x")
        expected = digest(payload)

        # The filler forces payload.bin high. Replacing the filler with one byte
        # releases a large low free range without altering payload.bin.
        run([tools["ntfscp"], "-f", str(image), str(filler), "/filler.bin"])
        run([tools["ntfscp"], "-f", str(image), str(payload), "/payload.bin"])
        run([tools["ntfscp"], "-f", str(image), str(tiny), "/filler.bin"])

        volume = ntfs_engine._open_volume(str(image), False)
        try:
            layout = ntfs_engine._read_layout(volume)
            before = {item.record_number: item for item in ntfs_engine._candidate_records(layout)}
            payload_candidate = max(before.values(), key=lambda item: item.clusters)
            before_lcn = payload_candidate.lowest_lcn
            before_high = payload_candidate.highest_lcn
        finally:
            ntfs_engine._close_volume(volume)

        ntfs_engine._stop_requested = False
        assert ntfs_engine.compact(str(image), journal) == 0
        assert not journal.exists()

        volume = ntfs_engine._open_volume(str(image), False)
        try:
            layout = ntfs_engine._read_layout(volume)
            after = {item.record_number: item for item in ntfs_engine._candidate_records(layout)}
            moved = after[payload_candidate.record_number]
            assert moved.highest_lcn < before_high
            assert moved.lowest_lcn <= before_lcn
        finally:
            ntfs_engine._close_volume(volume)

        run([tools["ntfscat"], "-f", str(image), "/payload.bin"], extracted)
        assert digest(extracted) == expected
        run([tools["ntfsresize"], "--check", "--force", "--force", str(image)])
    print("Real native NTFS relocation, SHA-256 and independent consistency test passed")


if __name__ == "__main__":
    main()
