#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Create portable, deliberately fragmented test files and directories.

"""Create deterministic fragmentation test data on any mounted writable filesystem.

The utility uses only normal filesystem calls. It therefore works with any
filesystem that Linux can mount read/write, including FAT, exFAT, NTFS, HFS+
and Amiga filesystems supported by the running kernel. Allocation policy varies
between filesystems, so the program creates many alternating holes and writes
several files round-robin to strongly encourage fragmented chains/extents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

BLOCK = 256 * 1024


def deterministic_block(file_index: int, chunk_index: int, size: int = BLOCK) -> bytes:
    seed = hashlib.sha256(f"linux-defragger:{file_index}:{chunk_index}".encode()).digest()
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def write_full(path: Path, size: int, marker: int) -> None:
    chunk = bytes([marker & 0xFF]) * BLOCK
    remaining = size
    with path.open("wb", buffering=0) as handle:
        while remaining:
            data = chunk[: min(len(chunk), remaining)]
            handle.write(data)
            remaining -= len(data)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create deliberately fragmented cross-filesystem test data."
    )
    parser.add_argument("directory", help="mounted writable parent directory")
    parser.add_argument("--anchors", type=int, default=64)
    parser.add_argument("--anchor-mib", type=int, default=2)
    parser.add_argument("--files", type=int, default=8)
    parser.add_argument("--chunks", type=int, default=64)
    parser.add_argument("--chunk-kib", type=int, default=256)
    parser.add_argument("--force", action="store_true", help="replace an existing test folder")
    args = parser.parse_args()

    parent = Path(args.directory).resolve()
    if not parent.is_dir():
        parser.error(f"not a directory: {parent}")
    if not os.access(parent, os.W_OK | os.X_OK):
        parser.error(f"directory is not writable: {parent}")

    root = parent / "LinuxDefragger-TestData"
    if root.exists():
        if not args.force:
            parser.error(f"test folder already exists: {root}; use --force to replace it")
        shutil.rmtree(root)
    anchors = root / "anchors"
    targets = root / "fragmented-files"
    directory_test = root / "fragmented-directory"
    anchors.mkdir(parents=True)
    targets.mkdir()
    directory_test.mkdir()

    anchor_size = max(1, args.anchor_mib) * 1024 * 1024
    chunk_size = max(4, args.chunk_kib) * 1024
    print(f"Creating {args.anchors} allocation anchors ({args.anchor_mib} MiB each)…", flush=True)
    for index in range(args.anchors):
        write_full(anchors / f"anchor-{index:04d}.bin", anchor_size, index)

    print("Deleting alternating anchors to create physical holes…", flush=True)
    for index in range(1, args.anchors, 2):
        (anchors / f"anchor-{index:04d}.bin").unlink()
    os.sync()

    print(
        f"Writing {args.files} target files round-robin in {args.chunks} × "
        f"{args.chunk_kib} KiB chunks…",
        flush=True,
    )
    handles = [
        (targets / f"fragmented-{index:02d}.bin").open("wb", buffering=0)
        for index in range(args.files)
    ]
    hashes = [hashlib.sha256() for _ in handles]
    try:
        for chunk_index in range(args.chunks):
            for file_index, handle in enumerate(handles):
                data = deterministic_block(file_index, chunk_index, chunk_size)
                handle.write(data)
                hashes[file_index].update(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Occupy some newly available allocation between rounds.
            if chunk_index < args.anchors // 2:
                write_full(
                    anchors / f"interleave-{chunk_index:04d}.bin",
                    max(BLOCK, anchor_size // 2),
                    0x80 + chunk_index,
                )
    finally:
        for handle in handles:
            handle.close()

    print("Expanding and punching holes through a directory allocation…", flush=True)
    for index in range(4096):
        (directory_test / f"entry-{index:05d}.txt").write_text(f"first {index}\n")
    for index in range(0, 4096, 2):
        (directory_test / f"entry-{index:05d}.txt").unlink()
    for index in range(4096, 8192):
        (directory_test / f"entry-{index:05d}.txt").write_text(f"second {index}\n")
    os.sync()

    print("Removing temporary anchors while retaining the fragmented targets…", flush=True)
    shutil.rmtree(anchors)
    os.sync()

    manifest = {
        "schema": 1,
        "generator": "linux-defragger-testdata",
        "target_files": [
            {
                "path": f"fragmented-files/fragmented-{index:02d}.bin",
                "size": args.chunks * chunk_size,
                "sha256": digest.hexdigest(),
            }
            for index, digest in enumerate(hashes)
        ],
        "directory_entries": len(list(directory_test.iterdir())),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Test data created in {root}")
    print("Unmount the volume, select it in Linux Defragger, and click Analyse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
