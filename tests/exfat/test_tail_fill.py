#!/usr/bin/python3
"""Verify exFAT pure tail-fill Compact and schema-2 recovery."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gui"))
from exfat_engine import EOC, Volume, fragments, journal_write  # noqa: E402


def verify_payload(path: Path, expected_chain: list[int]) -> None:
    volume = Volume(str(path))
    try:
        entry = volume.parse()[0]
        assert entry.clusters == expected_chain, entry.clusters
        assert [volume.read(volume.coff(c), 1) for c in entry.clusters] == [b"A", b"B", b"C"]
    finally:
        volume.close()


def make_schema2_journal(image: Path, journal: Path, switched: bool) -> None:
    volume = Volume(str(image), True)
    try:
        entry = volume.parse()[0]
        old_chain = list(entry.clusters)
        source, destination, index = 20, 4, 1
        new_chain = [10, 4, 11]
        touched = set(old_chain); touched.add(destination)
        obj = {
            "schema": 2, "device": str(image), "serial": volume.serial, "path": entry.path,
            "old_chain": old_chain, "new_chain": new_chain, "source": source,
            "destination": destination, "index": index, "old_nofat": bool(entry.nofat),
            "fat_before": {str(c): volume.fatget(c) for c in touched},
            "destination_bit_before": bool(volume.bit(destination)),
            "source_bit_before": bool(volume.bit(source)),
            "phase": "destination-ready",
        }
        journal_write(str(journal), obj)
        volume.write(volume.coff(destination), volume.read(volume.coff(source), volume.cs))
        volume.setbit(destination, True); volume.fatset(destination, 11)
        volume.flush_fat_bitmap(); volume.sync()
        if switched:
            volume.fatset(10, destination)
            volume.flush_fat_bitmap(); volume.sync()
            obj["phase"] = "switched"
            journal_write(str(journal), obj)
    finally:
        volume.close()


def main() -> None:
    compacted, base, rollback, forward = map(Path, sys.argv[1:5])
    verify_payload(compacted, [6, 4, 5])
    volume = Volume(str(compacted))
    try:
        entry = volume.parse()[0]
        assert fragments(entry.clusters) == 2
        assert not entry.nofat
        assert all(volume.bit(c) for c in range(2, 7))
        assert not any(volume.bit(c) for c in range(7, volume.cc + 2))
    finally:
        volume.close()

    shutil.copyfile(base, rollback)
    rollback_journal = rollback.with_suffix(".journal")
    make_schema2_journal(rollback, rollback_journal, switched=False)
    # The caller invokes recovery, then this script is run again with the recovered images.

    shutil.copyfile(base, forward)
    forward_journal = forward.with_suffix(".journal")
    make_schema2_journal(forward, forward_journal, switched=True)
    print("exFAT tail-fill Compact verification prepared")


if __name__ == "__main__":
    main()
