#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Validate native high-water extent splitting on a real NTFS image.

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
PAYLOAD_SIZE = 250 * 1024 * 1024


def run(command: list[str], output: Path | None = None) -> str:
    if output is None:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=600, check=False, text=True)
        diagnostic = result.stdout or ""
    else:
        with output.open("wb") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE,
                                    timeout=600, check=False)
        diagnostic = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{diagnostic}"
        )
    return diagnostic


def make_file(path: Path, size: int, patterned: bool = False) -> None:
    with path.open("wb") as stream:
        if not patterned:
            stream.truncate(size)
            return
        block = bytes((index * 37 + 11) & 0xFF for index in range(1024 * 1024))
        for _ in range(size // len(block)):
            stream.write(block)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def largest_candidate(image: Path) -> tuple[ntfs_engine.Candidate, int, int]:
    volume = ntfs_engine._open_volume(str(image), False)
    try:
        layout = ntfs_engine._read_layout(volume)
        candidate = max(ntfs_engine._candidate_records(layout), key=lambda item: item.clusters)
        source = max(
            (run for run in candidate.attribute.runs if run.lcn is not None),
            key=lambda run: int(run.lcn) + run.length,
        )
        free = ntfs_engine._free_runs_before(layout.bitmap, int(source.lcn))
        return candidate, max((run.length for run in free), default=0), sum(run.length for run in free)
    finally:
        ntfs_engine._close_volume(volume)


def main() -> None:
    tools = {name: shutil.which(name) for name in
             ("mkntfs", "ntfscp", "ntfscat", "ntfsresize")}
    if not all(tools.values()):
        print("Real split-extent NTFS validation skipped: independent utilities unavailable")
        return
    with tempfile.TemporaryDirectory(prefix="linux-defragger-native-split-ntfs-") as directory:
        tmp = Path(directory)
        image = tmp / "volume.img"
        journal = tmp / "native.journal"
        tiny = tmp / "tiny.bin"
        extracted = tmp / "payload-after.bin"
        with image.open("wb") as stream:
            stream.truncate(IMAGE_SIZE)
        run([tools["mkntfs"], "-F", "-Q", "-L", "LINUX_DEFRAG_SPLIT", str(image)])

        layout = [
            ("filler-a.bin", 150, False),
            ("separator-a.bin", 16, False),
            ("filler-b.bin", 150, False),
            ("separator-b.bin", 16, False),
            ("payload.bin", PAYLOAD_SIZE // (1024 * 1024), True),
        ]
        for name, mib, patterned in layout:
            source = tmp / name
            make_file(source, mib * 1024 * 1024, patterned)
            run([tools["ntfscp"], "-f", str(image), str(source), "/" + name])
        expected = digest(tmp / "payload.bin")
        tiny.write_bytes(b"x")
        for name in ("filler-a.bin", "filler-b.bin"):
            run([tools["ntfscp"], "-f", str(image), str(tiny), "/" + name])

        before, largest_hole, total_lower_free = largest_candidate(image)
        before_high = before.highest_lcn
        assert largest_hole < before.clusters
        assert total_lower_free >= before.clusters
        before_capacity = before.attribute.length - before.attribute.run_offset

        ntfs_engine._stop_requested = False
        assert ntfs_engine.compact(str(image), journal) == 0
        assert not journal.exists()

        after, _largest, _total = largest_candidate(image)
        assert after.record_number == before.record_number
        assert after.highest_lcn < before_high
        assert len(after.attribute.runs) >= 2
        assert after.attribute.length - after.attribute.run_offset >= before_capacity

        run([tools["ntfscat"], "-f", str(image), "/payload.bin"], extracted)
        assert digest(extracted) == expected
        run([tools["ntfsresize"], "--check", "--force", "--force", str(image)])
    print("Real NTFS split-extent relocation, MFT growth, SHA-256 and consistency test passed")


if __name__ == "__main__":
    main()
