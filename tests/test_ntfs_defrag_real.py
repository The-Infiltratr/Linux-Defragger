#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Validate native NTFS defragmentation on a genuinely formatted image.

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

IMAGE_SIZE = 512 * 1024 * 1024
PAYLOAD_SIZE = 32 * 1024 * 1024


def run(command: list[str], output: Path | None = None) -> str:
    if output is None:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=600, check=False, text=True,
        )
        diagnostic = result.stdout or ""
    else:
        with output.open("wb") as stream:
            result = subprocess.run(
                command, stdout=stream, stderr=subprocess.PIPE,
                timeout=600, check=False,
            )
        diagnostic = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{diagnostic}"
        )
    return diagnostic


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def largest_candidate(image: Path) -> ntfs_engine.Candidate:
    volume = ntfs_engine._open_volume(str(image), False)
    try:
        layout = ntfs_engine._read_layout(volume)
        return max(ntfs_engine._candidate_records(layout), key=lambda item: item.clusters)
    finally:
        ntfs_engine._close_volume(volume)


def main() -> None:
    tools = {name: shutil.which(name) for name in ("mkntfs", "ntfscp", "ntfscat", "ntfsresize")}
    if not all(tools.values()):
        print("Real native NTFS defragmentation skipped: independent utilities unavailable")
        return

    with tempfile.TemporaryDirectory(prefix="linux-defragger-native-real-defrag-") as directory:
        tmp = Path(directory)
        image = tmp / "volume.img"
        payload = tmp / "payload.bin"
        extracted = tmp / "payload-after.bin"
        journal = tmp / "native.journal"

        with image.open("wb") as stream:
            stream.truncate(IMAGE_SIZE)
        run([tools["mkntfs"], "-F", "-Q", "-L", "LINUX_DEFRAG_DEFRAG", str(image)])

        block = bytes((index * 29 + 17) & 0xFF for index in range(1024 * 1024))
        with payload.open("wb") as stream:
            for _ in range(PAYLOAD_SIZE // len(block)):
                stream.write(block)
        expected = digest(payload)
        run([tools["ntfscp"], "-f", str(image), str(payload), "/payload.bin"])

        # Create a valid two-extent file with the native journalled mover. The
        # destination is deliberately in the middle of the free area so ample
        # trailing space remains for the actual Defragment operation.
        volume = ntfs_engine._open_volume(str(image), True)
        try:
            layout = ntfs_engine._read_layout(volume)
            candidate = max(ntfs_engine._candidate_records(layout), key=lambda item: item.clusters)
            assert ntfs_engine._physical_fragment_count(candidate.attribute.runs) == 1
            source = candidate.attribute.runs[0]
            assert source.lcn is not None and source.length >= 4
            take = source.length // 2
            prefix = source.length - take
            minimum = int(source.lcn) + source.length + 8192
            maximum_end = volume.total_clusters - candidate.clusters - 8192
            destination = None
            for free in ntfs_engine._free_runs_before(layout.bitmap, maximum_end):
                if free.lcn is not None and int(free.lcn) >= minimum and free.length >= take:
                    destination = int(free.lcn)
                    break
            assert destination is not None
            move = ntfs_engine.ExtentMove(
                source_runs=(ntfs_engine.Run(int(source.lcn) + prefix, take),),
                destination_runs=(ntfs_engine.Run(destination, take),),
                new_runs=(
                    ntfs_engine.Run(source.lcn, prefix),
                    ntfs_engine.Run(destination, take),
                ),
            )
            ntfs_engine._move_extent(layout, candidate, move, journal)
        finally:
            ntfs_engine._close_volume(volume)

        fragmented = largest_candidate(image)
        assert ntfs_engine._physical_fragment_count(fragmented.attribute.runs) == 2
        run([tools["ntfsresize"], "--check", "--force", "--force", str(image)])

        ntfs_engine._stop_requested = False
        assert ntfs_engine.defragment(str(image), journal) == 0
        assert not journal.exists()

        defragged = largest_candidate(image)
        assert ntfs_engine._physical_fragment_count(defragged.attribute.runs) == 1
        run([tools["ntfscat"], "-f", str(image), "/payload.bin"], extracted)
        assert digest(extracted) == expected
        run([tools["ntfsresize"], "--check", "--force", "--force", str(image)])

    print("Real native NTFS defragmentation, SHA-256 and consistency test passed")


if __name__ == "__main__":
    main()
