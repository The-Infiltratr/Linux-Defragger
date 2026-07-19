#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Journalled Amiga OFS/FFS file and directory relocation engine.

"""Offline Amiga OFS/FFS compaction, defragmentation and recovery engine."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.devices import is_mounted
from core.journal import write_json_journal
from version import VERSION

_VENDOR_CANDIDATES = (SCRIPT_DIR / "vendor", SCRIPT_DIR.parent / "vendor")
_VENDOR = next((candidate for candidate in _VENDOR_CANDIDATES if candidate.is_dir()), None)
if _VENDOR is not None and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.ADFSDir import ADFSDir
from amitools.fs.ADFSFile import ADFSFile
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.BlkDevFactory import BlkDevFactory
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.block.UserDirBlock import UserDirBlock
from amitools.fs.block.DirCacheBlock import DirCacheBlock

STOP = False


class Error(RuntimeError):
    pass


def on_signal(_sig, _frame):
    global STOP
    STOP = True
    print("interrupt requested; stopping after the active journalled transaction", flush=True)


def mounted(path: str) -> bool:
    return is_mounted(path)

def open_block_device(path: str, read_only: bool = False):
    suffix = path.lower()
    if suffix.endswith((".adf", ".adz", ".adf.gz", ".hdf", ".hdz", ".hdf.gz", ".rdb", ".rdisk")):
        return BlkDevFactory().open(path, read_only=read_only)
    blk = RawBlockDevice(path, read_only=read_only, block_bytes=512)
    blk.open()
    return blk


def open_volume(path: str, read_only: bool = False):
    blkdev = open_block_device(path, read_only)
    volume = ADFSVolume(blkdev)
    try:
        volume.open()
    except Exception:
        blkdev.close()
        raise
    return blkdev, volume


def sync_volume(blkdev, volume) -> None:
    volume.bitmap.write()
    blkdev.flush()
    image = getattr(blkdev, "img_file", None)
    handle = getattr(image, "fobj", None)
    if handle is not None:
        try:
            os.fsync(handle.fileno())
        except (OSError, AttributeError):
            pass


def journal_write(path: str, record: dict) -> None:
    write_json_journal(path, record)

def extent_count(blocks: list[int]) -> int:
    if not blocks:
        return 0
    return 1 + sum(1 for left, right in zip(blocks, blocks[1:]) if right != left + 1)


def find_run(bitmap, length: int, exclude: set[int] | None = None) -> int | None:
    exclude = exclude or set()
    start = None
    run = 0
    for block in range(bitmap.blkdev.reserved, bitmap.blkdev.num_blocks):
        if bitmap.get_bit(block) and block not in exclude:
            if start is None:
                start = block
            run += 1
            if run >= length:
                return start
        else:
            start = None
            run = 0
    return None


def path_text(node) -> str:
    names: list[str] = []
    current = node
    while current.parent is not None and current.parent.parent is not None:
        names.append(current.name.get_unicode_name())
        current = current.parent
    if current.parent is not None:
        names.append(current.name.get_unicode_name())
    return "/".join(reversed(names))


def collect(volume):
    objects = []
    def walk(directory):
        directory.ensure_entries()
        for node in directory.entries:
            objects.append(node)
            if isinstance(node, ADFSDir):
                walk(node)
    walk(volume.root_dir)
    return objects


def find_node(volume, path: str):
    if not path:
        return volume.root_dir
    return volume.get_path_name(FSString(path))


def parent_switch(parent, old_node, new_block: int) -> None:
    parent.ensure_entries()
    key = old_node.name.hash(hash_size=parent.hash_size)
    chain = parent.name_hash[key]
    position = chain.index(old_node)
    if position == 0:
        parent.block.hash_table[key] = new_block
        parent.block.write()
    else:
        previous = chain[position - 1]
        previous.block.hash_chain = new_block
        previous.block.write()
    if parent.volume.is_dircache:
        record = parent.get_dircache_record(old_node.name.name)
        if record is None:
            raise Error("directory-cache record not found")
        record.entry = new_block
        parent.update_dircache_record(record, False)


def allocate_explicit(bitmap, blocks: list[int]) -> None:
    for block in blocks:
        if not bitmap.get_bit(block):
            raise Error(f"destination block {block} is not free")
    for block in blocks:
        bitmap.clr_bit(block)


def free_explicit(bitmap, blocks: list[int]) -> None:
    for block in blocks:
        bitmap.set_bit(block)


def move_file(path: str, volume, blkdev, node: ADFSFile, destination: list[int], journal: str) -> None:
    if node.block.comment_block_id:
        raise Error(f"{path_text(node)} uses an external long-name comment block")
    source = node.get_block_nums()
    record = {
        "schema": 1, "filesystem": "affs", "kind": "file", "path": path_text(node),
        "source": source, "destination": destination, "phase": "prepared",
        "dostype": int(volume.boot.dos_type),
    }
    journal_write(journal, record)
    allocate_explicit(volume.bitmap, destination)
    replacement = ADFSFile(volume, node.parent)
    replacement.set_file_data(bytes(node.get_file_data()))
    replacement.blocks_create_new(
        destination, node.block.name, node.block.hash_chain,
        node.parent.block.blk_num, node.meta_info,
    )
    sync_volume(blkdev, volume)
    record["phase"] = "destination-ready"
    journal_write(journal, record)
    if os.environ.get("LINUX_DEFRAGGER_TEST_CRASH_PHASE") == "destination-ready":
        os._exit(99)
    parent_switch(node.parent, node, destination[0])
    blkdev.flush()
    record["phase"] = "switched"
    journal_write(journal, record)
    if os.environ.get("LINUX_DEFRAGGER_TEST_CRASH_PHASE") == "switched":
        os._exit(98)
    free_explicit(volume.bitmap, source)
    sync_volume(blkdev, volume)
    os.unlink(journal)


def move_directory(path: str, volume, blkdev, node: ADFSDir, destination: list[int], journal: str) -> None:
    if node.block.comment_block_id:
        raise Error(f"{path_text(node)} uses an external long-name comment block")
    node.ensure_entries()
    source = node.get_block_nums()
    record = {
        "schema": 1, "filesystem": "affs", "kind": "directory", "path": path_text(node),
        "source": source, "destination": destination, "phase": "prepared",
        "dostype": int(volume.boot.dos_type),
    }
    journal_write(journal, record)
    allocate_explicit(volume.bitmap, destination)
    new_dir = UserDirBlock(blkdev, destination[0], volume.is_longname)
    extension = destination[1] if len(destination) > 1 else 0
    new_dir.create(
        node.parent.block.blk_num, node.block.name, node.block.protect,
        node.block.comment, node.block.mod_ts, node.block.hash_chain, extension,
    )
    new_dir.hash_table = list(node.block.hash_table)
    new_dir.write()
    if volume.is_dircache:
        for index, old_cache in enumerate(node.dcache_blks):
            next_cache = destination[index + 2] if index + 2 < len(destination) else 0
            new_cache = DirCacheBlock(blkdev, destination[index + 1])
            new_cache.create(destination[0], list(old_cache.records), next_cache)
            new_cache.write()
    sync_volume(blkdev, volume)
    record["phase"] = "destination-ready"
    journal_write(journal, record)
    if os.environ.get("LINUX_DEFRAGGER_TEST_CRASH_PHASE") == "destination-ready":
        os._exit(99)
    parent_switch(node.parent, node, destination[0])
    blkdev.flush()
    record["phase"] = "switched"
    journal_write(journal, record)
    if os.environ.get("LINUX_DEFRAGGER_TEST_CRASH_PHASE") == "switched":
        os._exit(98)
    # Repair child parent pointers after the directory becomes reachable at its new block.
    for child in node.entries:
        child.block.parent = destination[0]
        child.block.write()
    sync_volume(blkdev, volume)
    free_explicit(volume.bitmap, source)
    sync_volume(blkdev, volume)
    os.unlink(journal)


def recover(device: str, journal: str) -> None:
    if mounted(device):
        raise Error("refusing to recover a mounted Amiga filesystem")
    if not os.path.exists(journal):
        raise Error("no recovery journal")
    record = json.load(open(journal, "r", encoding="utf-8"))
    blkdev, volume = open_volume(device, False)
    try:
        node = find_node(volume, record["path"])
        source = [int(value) for value in record["source"]]
        destination = [int(value) for value in record["destination"]]
        if node is not None and node.block.blk_num == destination[0]:
            if record["kind"] == "directory":
                node.ensure_entries()
                for child in node.entries:
                    child.block.parent = destination[0]
                    child.block.write()
            free_explicit(volume.bitmap, source)
            for block in destination:
                volume.bitmap.clr_bit(block)
            sync_volume(blkdev, volume)
            print("Recovery completed by retaining the relocated object.")
        elif node is not None and node.block.blk_num == source[0]:
            free_explicit(volume.bitmap, destination)
            for block in source:
                volume.bitmap.clr_bit(block)
            sync_volume(blkdev, volume)
            print("Recovery completed by rolling back the destination blocks.")
        else:
            raise Error("journal object points to neither source nor destination")
        os.unlink(journal)
    finally:
        try:
            volume.close()
        finally:
            blkdev.close()


def operate(device: str, operation: str, journal: str, max_objects: int | None) -> None:
    if mounted(device):
        raise Error("refusing to modify a mounted Amiga filesystem")
    if os.path.exists(journal):
        raise Error("unfinished journal exists; run recover")
    moved = 0
    while not STOP:
        blkdev, volume = open_volume(device, False)
        try:
            objects = collect(volume)
            candidates = []
            for node in objects:
                if isinstance(node, ADFSFile):
                    blocks = list(node.data_blk_nums)
                    all_blocks = node.get_block_nums()
                    fragmented = extent_count(blocks) > 1
                elif isinstance(node, ADFSDir):
                    blocks = node.get_block_nums()
                    all_blocks = blocks
                    fragmented = extent_count(blocks) > 1
                else:
                    continue
                run = find_run(volume.bitmap, len(all_blocks), set(all_blocks))
                if run is None:
                    continue
                if operation == "defrag" and not fragmented:
                    continue
                if operation == "compact" and run >= min(all_blocks):
                    continue
                score = (-extent_count(blocks), -len(all_blocks), path_text(node)) if operation == "defrag" else (min(all_blocks), path_text(node))
                candidates.append((score, node, list(range(run, run + len(all_blocks)))))
            if not candidates:
                break
            candidates.sort(key=lambda item: item[0])
            _score, node, destination = candidates[0]
            kind = "DIR" if isinstance(node, ADFSDir) else "FILE"
            print(
                f"move: {kind} {path_text(node)} ({len(node.get_block_nums())} blocks, "
                f"{extent_count(node.get_block_nums())} extents) -> block {destination[0]}",
                flush=True,
            )
            if isinstance(node, ADFSFile):
                move_file(device, volume, blkdev, node, destination, journal)
            else:
                move_directory(device, volume, blkdev, node, destination, journal)
            moved += 1
        finally:
            try:
                volume.close()
            finally:
                blkdev.close()
        if max_objects and moved >= max_objects:
            break
    print(f"Relocated {moved} Amiga filesystem objects.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Linux Defragger Amiga filesystem engine")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("operation", choices=("defrag", "compact", "recover"))
    parser.add_argument("device")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-objects", type=int)
    parser.add_argument("--ram-buffer")
    parser.add_argument("--workers")
    parser.add_argument("--live-map-cells")
    parser.add_argument("--transaction-files")
    args = parser.parse_args()
    if not args.write or args.confirm != args.device:
        raise Error("write confirmation required")
    if args.operation == "recover":
        recover(args.device, args.journal)
    else:
        operate(args.device, args.operation, args.journal, args.max_objects or args.max_files)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        raise SystemExit(main())
    except Error as exc:
        print(f"affs-engine: {exc}", file=sys.stderr)
        raise SystemExit(1)
