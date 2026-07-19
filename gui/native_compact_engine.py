#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Kernel-journalled free-space compaction for ext4, XFS and Btrfs.

"""Native Linux filesystem compaction engine.

The FAT, exFAT and NTFS engines edit their own on-disk metadata while the
volume is offline. ext4 Compact now also works offline: it temporarily shrinks
the complete filesystem to its minimum size, forcing regular files, directory
blocks, journals and relocatable metadata below that boundary, and then restores
the original filesystem size. This is a true filesystem-wide packing pass rather
than the earlier regular-file-only EXT4_IOC_MOVE_EXT loop.

XFS continues to use temporary, unlinked space-collector files and kernel range
exchange. The collector occupies existing free runs; low collector extents are
used as donors for exchanges with the highest movable file extents.

Btrfs cannot exchange arbitrary physical file extents because every extent is
copy-on-write and back-referenced. Its compactor first runs a native filtered
balance to repack data and metadata into fewer block groups, then uses the
native online resize transaction to force the surviving chunks toward the
physical beginning before restoring the exact original filesystem size.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import heapq
import json
import os
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backends.base import Reader, u16le, u32le, u64le  # noqa: E402
from backends import ext4 as ext_backend  # noqa: E402
from backends import xfs as xfs_backend  # noqa: E402
from backends import btrfs as btrfs_backend  # noqa: E402
from version import VERSION  # noqa: E402

EXIT_STOPPED = 130
MAX_TRANSACTION_BYTES = 256 * 1024 * 1024
COPY_BUFFER = 8 * 1024 * 1024
# Keep enough unallocated blocks for extent-tree splits, journal credits and
# filesystem housekeeping while the collector owns the rest of the free map.
# The collector runs as root, so it must count f_bfree (all free blocks), not
# f_bavail (only the blocks offered to an unprivileged process).
COLLECTOR_FLOOR = 64 * 1024 * 1024
MAX_NO_PROGRESS = 8
MAX_EXTENT_COMPACT_PASSES = 32
MAX_EXT4_REPACK_ROUNDS = 4
MAX_BTRFS_COMPACT_ROUNDS = 3

# Generic inode flags returned by FS_IOC_FSGETXATTR.
FS_XFLAG_REALTIME = 0x00000001
FS_XFLAG_IMMUTABLE = 0x00000008
FS_XFLAG_APPEND = 0x00000010
FS_XFLAG_DAX = 0x00008000
UNMOVABLE_XFLAGS = FS_XFLAG_REALTIME | FS_XFLAG_IMMUTABLE | FS_XFLAG_APPEND | FS_XFLAG_DAX

# Linux mount flags.
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_NOATIME = 1024

# fallocate flags.
FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02

# FIEMAP flags.
FIEMAP_FLAG_SYNC = 0x00000001
FIEMAP_EXTENT_LAST = 0x00000001
FIEMAP_EXTENT_UNKNOWN = 0x00000002
FIEMAP_EXTENT_DELALLOC = 0x00000004
FIEMAP_EXTENT_ENCODED = 0x00000008
FIEMAP_EXTENT_DATA_INLINE = 0x00000200
FIEMAP_EXTENT_DATA_TAIL = 0x00000400
FIEMAP_EXTENT_UNWRITTEN = 0x00000800
FIEMAP_EXTENT_SHARED = 0x00002000
UNMOVABLE_FIEMAP_FLAGS = (
    FIEMAP_EXTENT_UNKNOWN
    | FIEMAP_EXTENT_DELALLOC
    | FIEMAP_EXTENT_ENCODED
    | FIEMAP_EXTENT_DATA_INLINE
    | FIEMAP_EXTENT_DATA_TAIL
    | FIEMAP_EXTENT_UNWRITTEN
    | FIEMAP_EXTENT_SHARED
)

# ioctl encoding from asm-generic/ioctl.h.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, kind: int | str, number: int, size: int) -> int:
    if isinstance(kind, str):
        kind = ord(kind)
    return (
        (direction << _IOC_DIRSHIFT)
        | (kind << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


FS_IOC_FIEMAP = _ioc(_IOC_READ | _IOC_WRITE, "f", 11, 32)
FS_IOC_FSGETXATTR = _ioc(_IOC_READ, "X", 31, 28)
EXT4_IOC_MOVE_EXT = _ioc(_IOC_READ | _IOC_WRITE, "f", 15, 40)
XFS_IOC_EXCHANGE_RANGE = _ioc(_IOC_WRITE, "X", 129, 40)
BTRFS_IOC_RESIZE = _ioc(_IOC_WRITE, 0x94, 3, 4096)
BTRFS_IOC_BALANCE_V2 = _ioc(_IOC_READ | _IOC_WRITE, 0x94, 32, 1024)
BTRFS_IOC_BALANCE_CTL = _ioc(_IOC_WRITE, 0x94, 33, 4)
BTRFS_IOC_BALANCE_PROGRESS = _ioc(_IOC_READ, 0x94, 34, 1024)

BTRFS_BALANCE_DATA = 1 << 0
BTRFS_BALANCE_SYSTEM = 1 << 1
BTRFS_BALANCE_METADATA = 1 << 2
BTRFS_BALANCE_ARGS_USAGE = 1 << 1
BTRFS_BALANCE_ARGS_DEVID = 1 << 2
BTRFS_BALANCE_CTL_CANCEL = 2
BTRFS_BALANCE_ARG_SIZE = 136
BTRFS_BALANCE_DATA_OFFSET = 16
BTRFS_BALANCE_META_OFFSET = BTRFS_BALANCE_DATA_OFFSET + BTRFS_BALANCE_ARG_SIZE
BTRFS_BALANCE_SYS_OFFSET = BTRFS_BALANCE_META_OFFSET + BTRFS_BALANCE_ARG_SIZE
BTRFS_BALANCE_PROGRESS_OFFSET = BTRFS_BALANCE_SYS_OFFSET + BTRFS_BALANCE_ARG_SIZE

_stop_requested = False
_active_btrfs_fd: int | None = None


class CompactError(RuntimeError):
    pass


class SourceNotMovable(RuntimeError):
    pass


@dataclass
class ExtentPassResult:
    moved_bytes: int = 0
    transactions: int = 0
    runtime_skipped: int = 0
    blocked_reason: str = ""
    stopped: bool = False


@dataclass
class ExtentCompactSummary:
    moved_bytes: int = 0
    transactions: int = 0
    runtime_skipped: int = 0
    productive_passes: int = 0
    final_reason: str = ""
    stopped: bool = False


@dataclass
class FiemapExtent:
    logical: int
    physical: int
    length: int
    flags: int

    @property
    def physical_end(self) -> int:
        return self.physical + self.length


@dataclass
class SourceExtent:
    path: str
    logical: int
    physical: int
    length: int
    flags: int
    token: int
    generation: int = 0

    @property
    def physical_end(self) -> int:
        return self.physical + self.length


@dataclass(frozen=True)
class FreeRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class CollectorExtent:
    fd: int
    logical: int
    physical: int
    length: int
    flags: int

    @property
    def physical_end(self) -> int:
        return self.physical + self.length


class PrivateMount:
    def __init__(self, device: str, filesystem: str):
        self.device = os.path.realpath(device)
        self.filesystem = filesystem
        self.path: str | None = None
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                                    ctypes.c_ulong, ctypes.c_void_p]
        self.libc.mount.restype = ctypes.c_int
        self.libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.libc.umount2.restype = ctypes.c_int

    def __enter__(self) -> str:
        _assert_unmounted(self.device)
        base = Path("/run/linux-defragger")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = tempfile.mkdtemp(prefix="compact-", dir=str(base))
        options = b"rw"
        result = self.libc.mount(
            self.device.encode(), self.path.encode(), self.filesystem.encode(),
            MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_NOATIME,
            ctypes.c_char_p(options),
        )
        if result != 0:
            err = ctypes.get_errno()
            Path(self.path).rmdir()
            self.path = None
            raise CompactError(f"unable to mount {self.device} privately as {self.filesystem}: {os.strerror(err)}")
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.path is None:
            return
        try:
            root_fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if hasattr(os, "syncfs"):
                    os.syncfs(root_fd)
                else:
                    os.sync()
            finally:
                os.close(root_fd)
        except OSError:
            pass
        for _attempt in range(5):
            if self.libc.umount2(self.path.encode(), 0) == 0:
                break
            err = ctypes.get_errno()
            if err != errno.EBUSY:
                break
            time.sleep(0.2)
        try:
            Path(self.path).rmdir()
        except OSError:
            pass
        self.path = None


class SpaceCollector:
    """Unlinked files that temporarily own all existing free extents."""

    def __init__(self, mountpoint: str, block_size: int):
        self.mountpoint = mountpoint
        self.block_size = block_size
        self.workspace = Path(mountpoint) / f".linux-defragger-compact-{os.getpid()}"
        self.workspace.mkdir(mode=0o700)
        self.fds: list[int] = []
        self._counter = 0
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
        self.libc.fallocate.restype = ctypes.c_int

    def close(self) -> None:
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()
        try:
            self.workspace.rmdir()
        except OSError:
            pass

    def _new_unlinked_file(self, prefix: str) -> int:
        path = self.workspace / f"{prefix}-{self._counter:08d}"
        self._counter += 1
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.unlink(path)
        self.fds.append(fd)
        return fd

    def _fallocate(self, fd: int, mode: int, offset: int, length: int) -> None:
        if length <= 0:
            return
        if self.libc.fallocate(fd, mode, offset, length) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

    def fill_available(self) -> tuple[int, int]:
        fd = self._new_unlinked_file("collector")
        allocated = 0
        transactions = 0
        current = 0
        while True:
            stats = os.statvfs(self.mountpoint)
            # Compact is executed by the privileged helper.  f_bavail excludes
            # ext4's root-reserved pool and previously left those blocks as
            # visible white holes that could never be selected as destinations.
            # f_bfree is the total free-block count; fallocate remains the final
            # authority and the retry loop safely backs off when a filesystem
            # cannot make every reported block available to this file.
            available = stats.f_bfree * stats.f_frsize
            target = max(0, available - COLLECTOR_FLOOR)
            target -= target % self.block_size
            if target < self.block_size:
                break
            attempt = min(target, 1024 * 1024 * 1024)
            attempt -= attempt % self.block_size
            success = False
            while attempt >= self.block_size:
                try:
                    self._fallocate(fd, 0, current, attempt)
                    current += attempt
                    allocated += attempt
                    transactions += 1
                    success = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
                        raise
                    attempt //= 2
                    attempt -= attempt % self.block_size
            if not success:
                break
        os.fsync(fd)
        return allocated, transactions

    def owned_extents(self, device_bytes: int) -> list[CollectorExtent]:
        """Return the collector's allocated physical extents with their file offsets.

        These extents are the original filesystem free ranges.  Compact uses them
        directly as exchange donors instead of punching them free and attempting a
        second fallocate, which can fail with ENOSPC while the collector owns the
        rest of the free-space map.
        """
        result: list[CollectorExtent] = []
        for fd in self.fds:
            for extent in fiemap(fd):
                length = min(extent.length, max(0, device_bytes - extent.physical))
                length -= length % self.block_size
                if length <= 0:
                    continue
                result.append(CollectorExtent(
                    fd=fd,
                    logical=extent.logical,
                    physical=extent.physical,
                    length=length,
                    flags=extent.flags,
                ))
        result.sort(key=lambda item: (item.physical, item.logical, item.fd))
        return result

    def verify_slice(self, item: CollectorExtent, logical: int, physical: int, length: int) -> None:
        """Confirm that an unconsumed collector slice still owns the expected blocks."""
        mapped = fiemap(item.fd, logical, length)
        for extent in mapped:
            if extent.logical <= logical < extent.logical + extent.length:
                actual = extent.physical + (logical - extent.logical)
                available = extent.length - (logical - extent.logical)
                if actual == physical and available >= length:
                    return
                break
        raise CompactError(
            "the temporary space collector no longer owns the expected low physical "
            f"range at byte {physical:,}; stopped before changing a file mapping"
        )


def _signal_handler(_signum, _frame) -> None:
    global _stop_requested
    # Resize and extent-exchange ioctls are kernel-journalled.  SIGINT asks the
    # active syscall to return at its next interruptible boundary; cleanup code
    # then restores the original Btrfs size before the process exits.
    _stop_requested = True


def _emit_live_range(source_physical: int, destination_physical: int,
                     length: int, moved_total: int, pass_number: int,
                     live_cells: int) -> None:
    """Tell the GUI how the logical allocation picture changed.

    The temporary collector physically owns all nominally free blocks while a
    pass is active.  Exposing that temporary state would make the entire map
    appear used.  Instead, report the intended compaction view: the low range
    becomes allocated and the old high source range becomes free.
    """
    if live_cells <= 0 or length <= 0:
        return
    payload = {
        "source_start_byte": int(source_physical),
        "destination_start_byte": int(destination_physical),
        "length_bytes": int(length),
        "moved_total_bytes": int(moved_total),
        "pass": int(pass_number),
    }
    print("@@LIVE_RANGE " + json.dumps(payload, separators=(",", ":")), flush=True)


def _assert_unmounted(device: str) -> None:
    st = os.stat(device)
    if not stat.S_ISBLK(st.st_mode):
        raise CompactError("ext4, XFS and Btrfs Compact currently require a real block-device partition")
    major_minor = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CompactError(f"cannot verify mount state: {exc}") from exc
    for line in lines:
        fields = line.split()
        if len(fields) > 2 and fields[2] == major_minor:
            raise CompactError(f"{device} is already mounted; Compact requires an unmounted volume")


def fiemap(fd: int, start: int = 0, length: int = (1 << 64) - 1,
           batch: int = 512) -> list[FiemapExtent]:
    result: list[FiemapExtent] = []
    cursor = start
    end_limit = (1 << 64) - 1 if length == (1 << 64) - 1 else start + length
    while cursor < end_limit:
        header = struct.pack("=QQIIII", cursor, end_limit - cursor, FIEMAP_FLAG_SYNC, 0, batch, 0)
        buffer = bytearray(header + b"\0" * (batch * 56))
        fcntl.ioctl(fd, FS_IOC_FIEMAP, buffer, True)
        _fm_start, _fm_length, _flags, mapped, _count, _reserved = struct.unpack_from("=QQIIII", buffer, 0)
        if mapped == 0:
            break
        last_end = cursor
        saw_last = False
        for index in range(mapped):
            pos = 32 + index * 56
            logical, physical, extent_length, _r0, _r1, flags, _x0, _x1, _x2 = struct.unpack_from(
                "=QQQQQIIII", buffer, pos
            )
            if extent_length <= 0:
                raise CompactError("FIEMAP returned a zero-length extent")
            result.append(FiemapExtent(logical, physical, extent_length, flags))
            last_end = max(last_end, logical + extent_length)
            saw_last = saw_last or bool(flags & FIEMAP_EXTENT_LAST)
        if saw_last or last_end <= cursor:
            break
        cursor = last_end
    return result


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[FreeRange]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [FreeRange(start, end) for start, end in merged]


def ext4_compact_geometry(device: str) -> tuple[int, int]:
    with Reader(device) as reader:
        sb = reader.read(1024, 1024)
        if u16le(sb, 56) != 0xEF53:
            raise CompactError("not an ext filesystem")
        block_size = 1024 << u32le(sb, 24)
        compat = u32le(sb, 92)
        incompat = u32le(sb, 96)
        ro_compat = u32le(sb, 100)
        has_64bit = bool(incompat & ext_backend._EXT4_FEATURE_INCOMPAT_64BIT)
        blocks = u32le(sb, 4) | ((u32le(sb, 0x150) << 32) if has_64bit else 0)
        if not blocks or block_size < 1024 or block_size > 65536:
            raise CompactError("invalid ext geometry")
        if not (incompat & ext_backend._EXT4_FEATURE_INCOMPAT_EXTENTS):
            raise CompactError("Compact supports ext4 extent-format filesystems, not ext2/ext3 indirect blocks")
        if ro_compat & ext_backend._EXT4_FEATURE_RO_COMPAT_BIGALLOC:
            raise CompactError("ext4 bigalloc Compact is not yet supported")
        return block_size, blocks * block_size


def _run_ext4_tool(command: list[str], accepted: set[int] | None = None) -> int:
    """Run one offline e2fsprogs stage while streaming its output to the GUI."""
    accepted = {0} if accepted is None else accepted
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        raise CompactError(f"cannot execute {command[0]}: {exc}") from exc
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip("\n"), flush=True)
    result = process.wait()
    if result not in accepted:
        raise CompactError(
            f"{' '.join(command)} failed with exit status {result}"
        )
    return result


def compact_ext4_offline(device: str, live_cells: int = 0) -> int:
    """Iteratively pack ext4 data, directories and relocatable metadata.

    A minimum-size shrink clears the physical tail and relocates directory and
    metadata blocks. After each restore, the regular-file extent exchanger fills
    the lower holes left inside that minimum image. Shrinking again can then move
    the remaining directory and metadata allocations below the newly lowered
    file boundary. The process stops at a fixed point.
    """
    _assert_unmounted(device)
    e2fsck = shutil.which("e2fsck")
    resize2fs = shutil.which("resize2fs")
    if not e2fsck or not resize2fs:
        raise CompactError(
            "ext4 filesystem-wide Compact requires e2fsprogs (e2fsck and resize2fs)"
        )

    block_size, original_bytes = ext4_compact_geometry(device)
    original_blocks = original_bytes // block_size
    restored = True
    final_boundary = original_blocks
    total_file_moved = 0
    total_transactions = 0
    rounds_completed = 0

    print(
        "EXT4 Compact is performing an iterative offline filesystem-wide repack. "
        "Each round shrinks the filesystem to its minimum valid size, restores it, "
        "then fills the remaining low holes with higher regular-file extents. The "
        "next shrink relocates directories and metadata around that denser file layout.",
        flush=True,
    )
    print(
        f"Original ext4 size: {original_blocks:,} blocks of {block_size:,} bytes "
        f"({original_bytes / (1024**3):.2f} GiB). The partition itself will not be changed.",
        flush=True,
    )
    print("2.00 percent completed", flush=True)

    try:
        print(
            "EXT4 Compact phase 1: mandatory offline filesystem check and directory optimisation.",
            flush=True,
        )
        # -D rebuilds/optimises directory indexes and can collapse avoidable
        # directory-block fragmentation before the packing rounds begin.
        _run_ext4_tool([e2fsck, "-f", "-D", "-p", device], {0, 1})
        print("8.00 percent completed", flush=True)

        previous_boundary: int | None = None
        need_final_shrink = False
        for round_number in range(1, MAX_EXT4_REPACK_ROUNDS + 1):
            if _stop_requested:
                print("Stop requested before the next EXT4 repack round.", flush=True)
                return EXIT_STOPPED

            print(
                f"EXT4 repack round {round_number}/{MAX_EXT4_REPACK_ROUNDS}: shrinking to "
                "the minimum valid filesystem size so every allocation above that boundary "
                "is relocated lower.",
                flush=True,
            )
            restored = False
            _run_ext4_tool([resize2fs, "-M", "-p", device])
            _block_size_after, shrunk_bytes = ext4_compact_geometry(device)
            shrunk_blocks = shrunk_bytes // block_size
            if shrunk_blocks > original_blocks:
                raise CompactError("resize2fs reported a filesystem larger than its original size")
            final_boundary = shrunk_blocks
            print(
                f"EXT4 round {round_number} minimum boundary: {shrunk_blocks:,} blocks "
                f"({shrunk_bytes / (1024**3):.2f} GiB).",
                flush=True,
            )

            print(
                f"EXT4 repack round {round_number}: restoring the exact original filesystem "
                "size without changing the partition.",
                flush=True,
            )
            _run_ext4_tool([resize2fs, "-p", device, str(original_blocks)])
            restored = True
            rounds_completed = round_number

            if _stop_requested:
                print("Stop requested after the filesystem was restored safely.", flush=True)
                return EXIT_STOPPED

            print(
                f"EXT4 repack round {round_number}: filling lower free holes with higher "
                "regular-file extents before the next offline shrink.",
                flush=True,
            )
            file_summary = _run_extent_compaction(
                device, "ext4", live_cells, embedded=True
            )
            total_file_moved += file_summary.moved_bytes
            total_transactions += file_summary.transactions
            if file_summary.stopped:
                return EXIT_STOPPED

            percent = min(88.0, 8.0 + 80.0 * round_number / MAX_EXT4_REPACK_ROUNDS)
            print(f"{percent:.2f} percent completed", flush=True)

            boundary_stable = previous_boundary is not None and shrunk_blocks >= previous_boundary
            previous_boundary = shrunk_blocks
            if file_summary.moved_bytes == 0:
                print(
                    f"EXT4 reached a fixed point after round {round_number}: no ordinary file "
                    "extent can be moved into any lower free range.",
                    flush=True,
                )
                need_final_shrink = False
                break
            need_final_shrink = True
            if boundary_stable:
                print(
                    "The minimum boundary did not fall in this round, but file allocations "
                    "were still moved lower; one more offline shrink is required to relocate "
                    "directory and metadata blocks around the new layout.",
                    flush=True,
                )
        else:
            print(
                f"EXT4 reached the safety limit of {MAX_EXT4_REPACK_ROUNDS} repack rounds.",
                flush=True,
            )

        if need_final_shrink:
            print(
                "EXT4 final consolidation: applying one last minimum-size shrink after the "
                "last productive regular-file packing pass.",
                flush=True,
            )
            restored = False
            _run_ext4_tool([resize2fs, "-M", "-p", device])
            _block_size_after, shrunk_bytes = ext4_compact_geometry(device)
            final_boundary = shrunk_bytes // block_size
            _run_ext4_tool([resize2fs, "-p", device, str(original_blocks)])
            restored = True

        print("EXT4 final read-only verification.", flush=True)
        # Do not let the verifier allocate anything on the restored full-size
        # geometry. All corrective and directory-optimisation work happened
        # before the packing rounds; this final pass is deliberately read-only.
        _run_ext4_tool([e2fsck, "-f", "-n", device])
        final_block_size, final_bytes = ext4_compact_geometry(device)
        if final_block_size != block_size or final_bytes != original_bytes:
            raise CompactError(
                "ext4 verification completed, but the original filesystem size was not restored"
            )

        cleared_tail = max(0, original_blocks - final_boundary) * block_size
        print(
            f"EXT4 filesystem-wide Compact completed after {rounds_completed:,} repack round(s). "
            f"Regular-file packing moved {total_file_moved / (1024**3):.2f} GiB in "
            f"{total_transactions:,} kernel-journalled exchanges. All ordinary file and "
            f"directory allocations now fit below block {max(0, final_boundary - 1):,}; "
            f"{cleared_tail / (1024**3):.2f} GiB of physical tail was cleared.",
            flush=True,
        )
        print(
            "Allocations beyond that boundary can only be ext4 block-group structures "
            "created or required by the restored full-size filesystem, not ordinary files.",
            flush=True,
        )
        print("100.00 percent completed", flush=True)
        return EXIT_STOPPED if _stop_requested else 0
    finally:
        if not restored:
            try:
                _current_block_size, current_bytes = ext4_compact_geometry(device)
                current_blocks = current_bytes // block_size
                if current_blocks < original_blocks:
                    print(
                        "EXT4 Compact cleanup: restoring the original filesystem size after an "
                        "interrupted or failed shrink stage.",
                        flush=True,
                    )
                    _run_ext4_tool([resize2fs, "-p", device, str(original_blocks)])
            except Exception as restore_exc:
                print(
                    "CRITICAL: ext4 remains at its smaller but valid filesystem size because "
                    f"automatic restoration failed: {restore_exc}",
                    file=sys.stderr, flush=True,
                )

def xfs_compact_geometry(device: str) -> tuple[int, int]:
    with Reader(device) as reader:
        sb = reader.read(0, 512)
        if sb[:4] != xfs_backend._XFS_SB_MAGIC:
            raise CompactError("not an XFS filesystem")
        geometry = xfs_backend._XfsGeometry(sb, reader.size)
        return geometry.block_size, geometry.dblocks * geometry.block_size


def ext4_free_ranges(device: str) -> tuple[int, list[FreeRange], int]:
    with Reader(device) as reader:
        sb = reader.read(1024, 1024)
        if u16le(sb, 56) != 0xEF53:
            raise CompactError("not an ext filesystem")
        block_size = 1024 << u32le(sb, 24)
        incompat = u32le(sb, 96)
        has_64bit = bool(incompat & ext_backend._EXT4_FEATURE_INCOMPAT_64BIT)
        blocks = u32le(sb, 4) | ((u32le(sb, 0x150) << 32) if has_64bit else 0)
        first_data = u32le(sb, 20)
        per_group = u32le(sb, 32)
        desc_size = max(32, u16le(sb, 0xFE) if has_64bit else 32)
        if not blocks or not per_group:
            raise CompactError("invalid ext geometry")
        groups = (blocks - first_data + per_group - 1) // per_group
        desc_off = (2 if block_size == 1024 else 1) * block_size
        free: list[tuple[int, int]] = []
        for group in range(groups):
            desc = reader.read(desc_off + group * desc_size, desc_size)
            bitmap_block = u32le(desc, 0)
            if has_64bit and desc_size >= 64:
                bitmap_block |= u32le(desc, 32) << 32
            if bitmap_block <= 0 or bitmap_block >= blocks:
                continue
            bitmap = reader.read(bitmap_block * block_size, block_size)
            group_start = first_data + group * per_group
            count = min(per_group, blocks - group_start)
            run_start: int | None = None
            for bit in range(count):
                allocated = bool(bitmap[bit >> 3] & (1 << (bit & 7)))
                if not allocated and run_start is None:
                    run_start = bit
                elif allocated and run_start is not None:
                    free.append(((group_start + run_start) * block_size, (group_start + bit) * block_size))
                    run_start = None
            if run_start is not None:
                free.append(((group_start + run_start) * block_size, (group_start + count) * block_size))
        return block_size, _merge_ranges(free), blocks * block_size


def xfs_free_ranges(device: str) -> tuple[int, list[FreeRange], int]:
    with Reader(device) as reader:
        sb = reader.read(0, 512)
        if sb[:4] != xfs_backend._XFS_SB_MAGIC:
            raise CompactError("not an XFS filesystem")
        geometry = xfs_backend._XfsGeometry(sb, reader.size)
        free, _details, _blocks = xfs_backend._free_space(reader, geometry)
        return geometry.block_size, _merge_ranges(
            (start * geometry.block_size, end * geometry.block_size) for start, end in free
        ), geometry.dblocks * geometry.block_size


def _file_xflags(fd: int) -> int:
    buffer = bytearray(28)
    try:
        fcntl.ioctl(fd, FS_IOC_FSGETXATTR, buffer, True)
    except OSError as exc:
        if exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL):
            return 0
        raise
    return struct.unpack_from("=I", buffer, 0)[0]


def scan_sources(mountpoint: str, block_size: int, device_bytes: int,
                 excluded: Path) -> tuple[list[SourceExtent], int, int]:
    heap: list[tuple[int, int, int, SourceExtent]] = []
    token = 0
    files = 0
    skipped_extents = 0
    stack = [Path(mountpoint)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if path == excluded:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
                    except OSError:
                        continue
                    try:
                        files += 1
                        if _file_xflags(fd) & UNMOVABLE_XFLAGS:
                            skipped_extents += 1
                            continue
                        for extent in fiemap(fd):
                            if extent.flags & UNMOVABLE_FIEMAP_FLAGS:
                                skipped_extents += 1
                                continue
                            if (extent.logical % block_size or extent.physical % block_size
                                    or extent.length % block_size):
                                skipped_extents += 1
                                continue
                            logical = extent.logical
                            physical = extent.physical
                            length = extent.length
                            if length <= 0 or physical < 0 or physical + length > device_bytes:
                                skipped_extents += 1
                                continue
                            source = SourceExtent(str(path), logical, physical, length,
                                                  extent.flags, token)
                            heapq.heappush(heap, (-source.physical_end, token, source.generation, source))
                            token += 1
                    finally:
                        os.close(fd)
        except OSError:
            continue
    return heap, files, skipped_extents


def _pop_high_source(heap: list[tuple[int, int, int, SourceExtent]], above: int) -> SourceExtent | None:
    while heap:
        _neg_end, _token, generation, source = heapq.heappop(heap)
        if generation != source.generation or source.length <= 0:
            continue
        if source.physical_end <= above:
            return None
        return source
    return None


def _requeue_source(heap: list[tuple[int, int, int, SourceExtent]], source: SourceExtent) -> None:
    source.generation += 1
    heapq.heappush(heap, (-source.physical_end, source.token, source.generation, source))


def _verify_source(fd: int, source: SourceExtent, logical: int, physical: int, length: int) -> None:
    extents = fiemap(fd, logical, length)
    for extent in extents:
        if extent.logical <= logical and extent.logical + extent.length >= logical + length:
            actual = extent.physical + (logical - extent.logical)
            if actual == physical and not (extent.flags & UNMOVABLE_FIEMAP_FLAGS):
                return
    raise CompactError(f"source extent changed before relocation: {source.path}")


def _copy_range(source_fd: int, source_offset: int, donor_fd: int, length: int,
                donor_offset: int = 0) -> None:
    copied = 0
    file_size = os.fstat(source_fd).st_size
    while copied < length:
        if _stop_requested and copied == 0:
            raise InterruptedError
        take = min(COPY_BUFFER, length - copied)
        available = max(0, min(take, file_size - (source_offset + copied)))
        data = os.pread(source_fd, available, source_offset + copied) if available else b""
        if len(data) != available:
            raise CompactError("short read while staging a file extent")
        if available < take:
            data += b"\0" * (take - available)
        written = 0
        while written < len(data):
            amount = os.pwrite(donor_fd, data[written:], donor_offset + copied + written)
            if amount <= 0:
                raise CompactError("short write while staging a file extent")
            written += amount
        copied += take


def _ext4_exchange(source_fd: int, donor_fd: int, source_offset: int,
                   donor_offset: int, length: int, block_size: int) -> None:
    if source_offset % block_size or donor_offset % block_size or length % block_size:
        raise CompactError("unaligned ext4 extent exchange")
    request = bytearray(struct.pack(
        "=IIQQQQ", 0, donor_fd, source_offset // block_size, donor_offset // block_size,
        length // block_size, 0,
    ))
    try:
        fcntl.ioctl(source_fd, EXT4_IOC_MOVE_EXT, request, True)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.EOPNOTSUPP, errno.EPERM, errno.ETXTBSY):
            raise SourceNotMovable(os.strerror(exc.errno)) from exc
        raise
    _reserved, _fd, _orig, _donor, requested, moved = struct.unpack("=IIQQQQ", request)
    if moved != requested:
        raise CompactError(f"ext4 moved only {moved} of {requested} requested blocks")


def _xfs_exchange(source_fd: int, donor_fd: int, source_offset: int,
                  donor_offset: int, length: int) -> None:
    # DSYNC asks XFS to persist the atomic mapping exchange before returning.
    request = struct.pack("=iIQQQQ", donor_fd, 0, donor_offset, source_offset, length, 1 << 1)
    try:
        fcntl.ioctl(source_fd, XFS_IOC_EXCHANGE_RANGE, request)
    except OSError as exc:
        if exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP):
            raise CompactError(
                "this kernel or XFS volume does not support XFS_IOC_EXCHANGE_RANGE; "
                "range-level XFS compaction requires Linux 6.10 or newer with the feature enabled"
            ) from exc
        if exc.errno in (errno.EINVAL, errno.EPERM, errno.ETXTBSY):
            raise SourceNotMovable(os.strerror(exc.errno)) from exc
        raise


def _compact_extent_pass(mountpoint: str, filesystem: str, block_size: int,
                         device_bytes: int, live_cells: int, pass_number: int,
                         moved_before_pass: int, emit_progress: bool = True) -> ExtentPassResult:
    result = ExtentPassResult()
    collector = SpaceCollector(mountpoint, block_size)
    try:
        before = os.statvfs(mountpoint)
        total_free_before = before.f_bfree * before.f_frsize
        ordinary_free_before = before.f_bavail * before.f_frsize
        privileged_reserve = max(0, total_free_before - ordinary_free_before)
        allocated, collector_ops = collector.fill_available()
        gaps = collector.owned_extents(device_bytes)
        total_target = sum(item.length for item in gaps)
        print(
            f"{filesystem.upper()} Compact pass {pass_number}: temporary space collector "
            f"mapped {allocated / (1024**3):.2f} GiB of total free space in "
            f"{collector_ops} fallocate operations and {len(gaps):,} physical ranges.",
            flush=True,
        )
        if privileged_reserve:
            print(
                f"The collector included up to {privileged_reserve / (1024**3):.2f} GiB "
                "from the filesystem's privileged reserve instead of leaving it as "
                "unusable low-map gaps.",
                flush=True,
            )
        print(
            "Compact will exchange file mappings directly with those reserved low ranges; "
            "it will not allocate a second donor file.",
            flush=True,
        )
        if not gaps:
            result.blocked_reason = "no accessible free range requires work"
            return result

        heap, file_count, skipped = scan_sources(
            mountpoint, block_size, device_bytes, collector.workspace
        )
        print(
            f"Scanned {file_count:,} regular files; {len(heap):,} movable physical extents "
            f"were found and {skipped:,} unsupported extents or files were skipped.",
            flush=True,
        )
        if not heap:
            result.blocked_reason = "no movable regular-file extents were found"
            return result

        gap_bytes_completed = 0
        for gap in gaps:
            cursor = gap.physical
            donor_logical = gap.logical
            while cursor < gap.physical_end:
                if _stop_requested:
                    print(
                        "Stop requested; ending Compact between kernel-journalled transactions.",
                        flush=True,
                    )
                    result.stopped = True
                    return result

                source = _pop_high_source(heap, cursor)
                if source is None:
                    result.blocked_reason = (
                        f"no movable regular-file extent remains above the free range at byte {cursor:,}"
                    )
                    break
                available = gap.physical_end - cursor
                move = min(available, source.length, MAX_TRANSACTION_BYTES)
                move -= move % block_size
                if move <= 0:
                    result.blocked_reason = (
                        "the next source or destination is smaller than one filesystem block"
                    )
                    break
                source_offset = source.logical + source.length - move
                source_physical = source.physical + source.length - move
                destination_physical = cursor

                try:
                    source_fd = os.open(
                        source.path,
                        os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    )
                except OSError:
                    source.length = 0
                    result.runtime_skipped += 1
                    continue

                try:
                    _verify_source(source_fd, source, source_offset, source_physical, move)
                    collector.verify_slice(gap, donor_logical, cursor, move)
                    _copy_range(source_fd, source_offset, gap.fd, move, donor_logical)
                    os.fsync(gap.fd)
                    try:
                        if filesystem == "ext4":
                            _ext4_exchange(
                                source_fd, gap.fd, source_offset, donor_logical, move, block_size
                            )
                        else:
                            _xfs_exchange(source_fd, gap.fd, source_offset, donor_logical, move)
                    except SourceNotMovable as exc:
                        source.length = 0
                        result.runtime_skipped += 1
                        print(
                            f"Skipped one file extent that the {filesystem} kernel refused to "
                            f"exchange ({exc}); trying the next high extent.",
                            flush=True,
                        )
                        continue
                    os.fsync(source_fd)
                    os.fsync(gap.fd)
                finally:
                    os.close(source_fd)

                source.length -= move
                if source.length > 0:
                    _requeue_source(heap, source)
                result.moved_bytes += move
                result.transactions += 1
                cursor += move
                donor_logical += move
                gap_bytes_completed += move
                moved_total = moved_before_pass + result.moved_bytes
                _emit_live_range(
                    source_physical,
                    destination_physical,
                    move,
                    moved_total,
                    pass_number,
                    live_cells,
                )
                pass_percent = min(100.0, 100.0 * gap_bytes_completed / max(1, total_target))
                # A pass can expose new low gaps only after its collector is
                # released.  Reserve half of the remaining progress range for
                # each possible follow-up pass so the GUI never jumps backwards.
                base = 100.0 * (1.0 - (0.5 ** (pass_number - 1)))
                span = 100.0 * (0.5 ** pass_number)
                percent = min(99.0, base + span * (pass_percent / 100.0))
                print(
                    f"{filesystem.upper()} Compact: pasted {move / (1024**2):.1f} MiB "
                    f"from physical byte {source_physical:,} into byte {destination_physical:,}; "
                    f"pass {pass_number} total {result.moved_bytes / (1024**3):.2f} GiB, "
                    f"overall {moved_total / (1024**3):.2f} GiB.",
                    flush=True,
                )
                if emit_progress:
                    print(f"{percent:.2f} percent completed", flush=True)
            if result.blocked_reason:
                break

        if hasattr(os, "syncfs"):
            root_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.syncfs(root_fd)
            finally:
                os.close(root_fd)
        return result
    finally:
        collector.close()


def _run_extent_compaction(device: str, filesystem: str, live_cells: int = 0,
                           *, embedded: bool = False) -> ExtentCompactSummary:
    if filesystem == "ext4":
        block_size, device_bytes = ext4_compact_geometry(device)
    elif filesystem == "xfs":
        block_size, device_bytes = xfs_compact_geometry(device)
    else:
        raise CompactError(f"unsupported extent filesystem: {filesystem}")

    if not embedded:
        print(
            f"{filesystem.upper()} Compact will paste the highest movable regular-file extents "
            "into the lowest accessible free ranges. It does not try to reduce fragmentation.",
            flush=True,
        )
        print(
            "The engine repeats collector passes automatically until another pass can no "
            "longer move any regular-file allocation lower.",
            flush=True,
        )

    summary = ExtentCompactSummary()
    with PrivateMount(device, filesystem) as mountpoint:
        for pass_number in range(1, MAX_EXTENT_COMPACT_PASSES + 1):
            if _stop_requested:
                print("Stop requested before the next Compact pass.", flush=True)
                summary.stopped = True
                return summary
            result = _compact_extent_pass(
                mountpoint,
                filesystem,
                block_size,
                device_bytes,
                live_cells,
                pass_number,
                summary.moved_bytes,
                emit_progress=not embedded,
            )
            summary.moved_bytes += result.moved_bytes
            summary.transactions += result.transactions
            summary.runtime_skipped += result.runtime_skipped
            summary.final_reason = result.blocked_reason

            print(
                f"{filesystem.upper()} Compact pass {pass_number} moved "
                f"{result.moved_bytes / (1024**3):.2f} GiB in "
                f"{result.transactions:,} kernel-journalled transactions.",
                flush=True,
            )

            if result.stopped:
                summary.stopped = True
                print(
                    f"{filesystem.upper()} Compact stopped safely after moving "
                    f"{summary.moved_bytes / (1024**3):.2f} GiB in "
                    f"{summary.transactions:,} completed kernel-journalled transactions.",
                    flush=True,
                )
                return summary
            if result.moved_bytes <= 0:
                break

            summary.productive_passes += 1
            if hasattr(os, "syncfs"):
                root_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.syncfs(root_fd)
                finally:
                    os.close(root_fd)
        else:
            summary.final_reason = (
                f"the automatic safety limit of {MAX_EXTENT_COMPACT_PASSES} passes was reached"
            )

    if not embedded:
        print(
            f"{filesystem.upper()} Compact moved {summary.moved_bytes / (1024**3):.2f} GiB in "
            f"{summary.transactions:,} kernel-journalled transactions across "
            f"{summary.productive_passes:,} productive passes; "
            f"{summary.runtime_skipped:,} additional source extents were skipped at runtime.",
            flush=True,
        )
        if summary.final_reason:
            print(
                f"Compaction reached its current online boundary: {summary.final_reason}.",
                flush=True,
            )
        print(
            "All movable regular-file extents have been packed as low as the current "
            "filesystem metadata and directory allocations permit.",
            flush=True,
        )
    return summary


def compact_extent_filesystem(device: str, filesystem: str, live_cells: int = 0) -> int:
    summary = _run_extent_compaction(device, filesystem, live_cells)
    print("100.00 percent completed", flush=True)
    return EXIT_STOPPED if summary.stopped else 0

def _btrfs_super_geometry(device: str) -> tuple[int, int, int]:
    with Reader(device) as reader:
        sb = reader.read(btrfs_backend._SUPER_OFFSET, btrfs_backend._SUPER_SIZE)
        if sb[0x40:0x48] != btrfs_backend._BTRFS_MAGIC:
            raise CompactError("not a Btrfs filesystem")
        total = u64le(sb, 112)
        num_devices = u64le(sb, 136)
        devid = u64le(sb, 201)
        if num_devices != 1:
            raise CompactError("Btrfs Compact currently supports single-device filesystems only")
        if total <= 0:
            raise CompactError("invalid Btrfs filesystem size")
        return total, devid, reader.size


def _btrfs_layout(fd: int, total: int, devid: int) -> tuple[list[FreeRange], int, int, int, dict]:
    try:
        mapper, physical_ranges, stats = btrfs_backend.kernel_chunk_layout(fd, total, devid)
    except btrfs_backend.BackendError as exc:
        raise CompactError(str(exc)) from exc
    allocated = _merge_ranges(physical_ranges)
    gaps: list[FreeRange] = []
    cursor = 1024 * 1024
    for item in allocated:
        start = max(cursor, item.start)
        if start > cursor:
            gaps.append(FreeRange(cursor, start))
        cursor = max(cursor, item.end)
    highwater = max((item.end for item in allocated), default=cursor)
    allocated_bytes = sum(item.length for item in allocated)
    largest_chunk = max((chunk.length for chunk in mapper.chunks), default=256 * 1024 * 1024)
    return gaps, highwater, allocated_bytes, largest_chunk, stats


def _btrfs_resize(fd: int, devid: int, size: int) -> None:
    request = bytearray(4096)
    amount = f"{devid}:{size}".encode("ascii")
    if len(amount) >= 255:
        raise CompactError("Btrfs resize request is too long")
    request[8:8 + len(amount)] = amount
    request[8 + len(amount)] = 0
    fcntl.ioctl(fd, BTRFS_IOC_RESIZE, request, True)


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _candidate_shrink_targets(allocated: int, largest_chunk: int,
                              highwater: int) -> list[int]:
    """Return progressively tighter chunk-boundary targets.

    The first target is deliberately only one allocation-group step below the
    current high-water mark. Later targets approach the theoretical packed
    chunk size while retaining at least one large chunk of relocation workspace.
    """
    alignment = 256 * 1024 * 1024
    minimum = _align_up(1024 * 1024 + allocated, alignment)
    targets: list[int] = []

    for step in (64 * 1024 * 1024, alignment, 512 * 1024 * 1024, 1024**3):
        target = highwater - step
        target = (target // alignment) * alignment
        if minimum < target < highwater:
            targets.append(target)

    for reserve in (
        max(1024**3, largest_chunk * 2),
        max(512 * 1024 * 1024, largest_chunk),
        max(256 * 1024 * 1024, largest_chunk),
    ):
        target = _align_up(1024 * 1024 + allocated + reserve, alignment)
        if minimum < target < highwater:
            targets.append(target)

    return sorted(set(targets), reverse=True)


def _btrfs_balance_request(devid: int, usage: int = 100) -> bytearray:
    request = bytearray(1024)
    struct.pack_into("=Q", request, 0, BTRFS_BALANCE_DATA | BTRFS_BALANCE_METADATA)
    for offset in (BTRFS_BALANCE_DATA_OFFSET, BTRFS_BALANCE_META_OFFSET):
        struct.pack_into("=Q", request, offset + 8, usage)
        struct.pack_into("=Q", request, offset + 16, devid)
        struct.pack_into(
            "=Q", request, offset + 64,
            BTRFS_BALANCE_ARGS_USAGE | BTRFS_BALANCE_ARGS_DEVID,
        )
    return request


def _btrfs_balance_progress(fd: int) -> tuple[int, int, int] | None:
    request = bytearray(1024)
    try:
        fcntl.ioctl(fd, BTRFS_IOC_BALANCE_PROGRESS, request, True)
    except OSError as exc:
        if exc.errno in (errno.ENOTCONN, errno.ENOENT, errno.EINVAL):
            return None
        raise
    return struct.unpack_from("=QQQ", request, BTRFS_BALANCE_PROGRESS_OFFSET)


def _btrfs_cancel_balance(fd: int) -> None:
    try:
        fcntl.ioctl(
            fd, BTRFS_IOC_BALANCE_CTL,
            bytearray(struct.pack("=i", BTRFS_BALANCE_CTL_CANCEL)), True,
        )
    except OSError as exc:
        if exc.errno not in (errno.ENOTCONN, errno.ENOENT, errno.EINVAL):
            raise


def _run_btrfs_balance(fd: int, devid: int, round_number: int) -> bool:
    """Run a full native data/metadata balance with live progress and cancellation."""
    request = _btrfs_balance_request(devid, 100)
    outcome: dict[str, BaseException | int] = {}

    def worker() -> None:
        try:
            outcome["result"] = fcntl.ioctl(fd, BTRFS_IOC_BALANCE_V2, request, True)
        except BaseException as exc:  # propagated in the caller thread
            outcome["error"] = exc

    print(
        f"Btrfs Compact round {round_number}: repacking all data and metadata block groups "
        "with the native balance ioctl so partially used chunks can be released.",
        flush=True,
    )
    thread = threading.Thread(target=worker, name="linux-defragger-btrfs-balance", daemon=True)
    thread.start()
    cancel_sent = False
    last_completed = -1
    last_percent = -1.0
    while thread.is_alive():
        thread.join(0.35)
        if _stop_requested and not cancel_sent:
            print("Stop requested; cancelling the active Btrfs balance transaction…", flush=True)
            _btrfs_cancel_balance(fd)
            cancel_sent = True
        try:
            progress = _btrfs_balance_progress(fd)
        except OSError:
            progress = None
        if progress is not None:
            expected, considered, completed = progress
            if completed != last_completed:
                print(
                    f"Btrfs balance progress: {completed:,} of {expected:,} selected chunks "
                    f"relocated; {considered:,} considered.",
                    flush=True,
                )
                last_completed = completed
            if expected:
                local = min(100.0, 100.0 * completed / expected)
                overall = min(89.0, 5.0 + (round_number - 1) * 28.0 + local * 0.20)
                if overall - last_percent >= 0.25:
                    print(f"{overall:.2f} percent completed", flush=True)
                    last_percent = overall

    error = outcome.get("error")
    if error is not None:
        if isinstance(error, OSError):
            if _stop_requested and error.errno in (errno.ECANCELED, errno.EINTR):
                return False
            if error.errno == errno.ENOSPC:
                print(
                    "Btrfs balance could not repack every selected chunk because temporary "
                    "workspace was exhausted; Compact will still attempt boundary shrinking "
                    "with the chunks that were released.",
                    flush=True,
                )
                return True
            raise CompactError(f"Btrfs balance failed: {os.strerror(error.errno)}") from error
        raise error
    if hasattr(os, "syncfs"):
        os.syncfs(fd)
    print("Btrfs data/metadata balance completed.", flush=True)
    return not _stop_requested

def compact_btrfs(device: str) -> int:
    global _active_btrfs_fd
    original_size, devid, device_size = _btrfs_super_geometry(device)
    if original_size > device_size > 0:
        raise CompactError("Btrfs filesystem size exceeds the block device")

    print(
        "Btrfs Compact performs a native balance-and-shrink repack. The balance stage "
        "consolidates live extents into fewer data and metadata chunks; the resize stage "
        "then forces those chunks toward the physical beginning before restoring the "
        "exact original filesystem size. File defragmentation is not invoked.",
        flush=True,
    )

    with PrivateMount(device, "btrfs") as mountpoint:
        fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        _active_btrfs_fd = fd
        restored = True
        total_cycles = 0
        try:
            gaps, highwater, allocated, largest_chunk, stats = _btrfs_layout(
                fd, original_size, devid
            )
            initial_highwater = highwater
            initial_allocated = allocated
            initial_gap_bytes = sum(gap.length for gap in gaps)
            print(
                f"Btrfs initial physical chunk layout: {allocated / (1024**3):.2f} GiB "
                f"allocated in {len(gaps):,} internal gap(s), high-water byte {highwater:,}. "
                f"Read through {stats.get('tree_search_calls', 0):,} kernel tree-search calls.",
                flush=True,
            )
            print("3.00 percent completed", flush=True)

            previous_highwater = highwater
            previous_allocated = allocated
            for round_number in range(1, MAX_BTRFS_COMPACT_ROUNDS + 1):
                if _stop_requested:
                    return EXIT_STOPPED
                balance_completed = _run_btrfs_balance(fd, devid, round_number)
                if not balance_completed and _stop_requested:
                    return EXIT_STOPPED

                gaps, highwater, allocated, largest_chunk, stats = _btrfs_layout(
                    fd, original_size, devid
                )
                print(
                    f"Btrfs round {round_number} after balance: allocated chunk space "
                    f"{previous_allocated / (1024**3):.2f} -> {allocated / (1024**3):.2f} GiB; "
                    f"chunk high-water byte {previous_highwater:,} -> {highwater:,}.",
                    flush=True,
                )

                round_reduction = 0
                shrink_attempted = False
                for target in _candidate_shrink_targets(allocated, largest_chunk, highwater):
                    if _stop_requested:
                        return EXIT_STOPPED
                    shrink_attempted = True
                    print(
                        f"Btrfs Compact shrink cycle {total_cycles + 1}: temporarily reducing "
                        f"the filesystem from {original_size / (1024**3):.2f} GiB to "
                        f"{target / (1024**3):.2f} GiB.",
                        flush=True,
                    )
                    shrunk = False
                    try:
                        _btrfs_resize(fd, devid, target)
                        shrunk = True
                        restored = False
                        if hasattr(os, "syncfs"):
                            os.syncfs(fd)
                        new_gaps, new_highwater, new_allocated, new_largest, new_stats = _btrfs_layout(
                            fd, original_size, devid
                        )
                        reduction = max(0, highwater - new_highwater)
                        gap_reduction = max(
                            0,
                            sum(gap.length for gap in gaps)
                            - sum(gap.length for gap in new_gaps),
                        )
                        total_cycles += 1
                        round_reduction += reduction
                        print(
                            f"Btrfs shrink cycle {total_cycles}: chunk high-water byte "
                            f"{highwater:,} -> {new_highwater:,}; internal chunk gaps reduced "
                            f"by {gap_reduction / (1024**3):.2f} GiB.",
                            flush=True,
                        )
                        gaps = new_gaps
                        highwater = new_highwater
                        allocated = new_allocated
                        largest_chunk = new_largest
                        stats = new_stats
                    except OSError as exc:
                        if exc.errno in (errno.ENOSPC, errno.EFBIG):
                            print(
                                f"Btrfs could not reach the temporary "
                                f"{target / (1024**3):.2f} GiB boundary. The preceding balance "
                                "did not release enough lower chunk workspace for this target.",
                                flush=True,
                            )
                            break
                        if _stop_requested and exc.errno in (errno.EINTR, errno.ECANCELED):
                            return EXIT_STOPPED
                        raise CompactError(
                            f"Btrfs resize failed: {os.strerror(exc.errno)}"
                        ) from exc
                    finally:
                        if shrunk:
                            try:
                                _btrfs_resize(fd, devid, original_size)
                                if hasattr(os, "syncfs"):
                                    os.syncfs(fd)
                                restored = True
                            except OSError as grow_exc:
                                restored = False
                                raise CompactError(
                                    "Btrfs data relocation completed but restoring the original "
                                    f"filesystem size failed: {os.strerror(grow_exc.errno)}"
                                ) from grow_exc

                    if round_reduction > 0:
                        # Re-read and rebalance from the new lower boundary rather than
                        # trying stale targets calculated from the old chunk layout.
                        break

                print(
                    f"{min(94.0, 12.0 + round_number * 27.0):.2f} percent completed",
                    flush=True,
                )
                improved = (
                    highwater < previous_highwater or allocated < previous_allocated
                )
                previous_highwater = highwater
                previous_allocated = allocated
                if not improved and (not shrink_attempted or round_reduction == 0):
                    print(
                        f"Btrfs reached a fixed point after round {round_number}; another full "
                        "balance did not free a lower chunk slot or reduce the physical boundary.",
                        flush=True,
                    )
                    break

            final_gaps, final_highwater, final_allocated, _largest, final_stats = _btrfs_layout(
                fd, original_size, devid
            )
            final_gap_bytes = sum(gap.length for gap in final_gaps)
            actual_reduction = max(0, initial_highwater - final_highwater)
            chunk_reduction = max(0, initial_allocated - final_allocated)
            print(
                f"Btrfs Compact completed {total_cycles:,} shrink-and-restore cycle(s). "
                f"Allocated chunk space {initial_allocated / (1024**3):.2f} -> "
                f"{final_allocated / (1024**3):.2f} GiB; chunk high-water byte "
                f"{initial_highwater:,} -> {final_highwater:,}, a physical tail reduction "
                f"of {actual_reduction / (1024**3):.2f} GiB.",
                flush=True,
            )
            print(
                f"Internal chunk gaps: {initial_gap_bytes / (1024**3):.2f} GiB -> "
                f"{final_gap_bytes / (1024**3):.2f} GiB; released chunk capacity "
                f"{chunk_reduction / (1024**3):.2f} GiB. Final layout required "
                f"{final_stats.get('tree_search_calls', 0):,} kernel tree-search calls.",
                flush=True,
            )
            if actual_reduction == 0 and chunk_reduction == 0:
                print(
                    "No effective Btrfs compaction was possible: every remaining chunk is "
                    "needed by the current data/metadata profile and no lower replacement "
                    "slot could be created.",
                    flush=True,
                )
            elif final_gaps:
                print(
                    "Btrfs is physically denser, but profile-constrained chunk allocations "
                    "still separate some free physical ranges.",
                    flush=True,
                )
            else:
                print("Btrfs physical chunks are packed with no internal gaps.", flush=True)
            print("100.00 percent completed", flush=True)
            return EXIT_STOPPED if _stop_requested else 0
        finally:
            if not restored:
                try:
                    _btrfs_resize(fd, devid, original_size)
                    if hasattr(os, "syncfs"):
                        os.syncfs(fd)
                except OSError:
                    pass
            _active_btrfs_fd = None
            os.close(fd)

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="linux-defragger-native-compact",
        description="Kernel-journalled free-space compaction for ext4, XFS and Btrfs.",
    )
    parser.add_argument("operation", choices=("compact",))
    parser.add_argument("device")
    parser.add_argument("--filesystem", required=True, choices=("ext4", "xfs", "btrfs"))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm")
    # Kept for command-line ABI compatibility with the other native engines.
    # ext4/XFS/Btrfs use kernel journals rather than a userspace journal file.
    parser.add_argument("--journal")
    parser.add_argument("--ram-buffer")
    parser.add_argument("--workers")
    parser.add_argument("--live-map-cells", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.write or args.confirm != args.device:
        raise CompactError("write mode requires --write --confirm DEVICE")
    if os.geteuid() != 0:
        raise CompactError("ext4, XFS and Btrfs Compact must run with administrator privileges")
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    print(f"Native Compact engine {VERSION}", flush=True)
    if args.filesystem == "btrfs":
        return compact_btrfs(args.device)
    if args.filesystem == "ext4":
        return compact_ext4_offline(args.device, args.live_map_cells)
    return compact_extent_filesystem(args.device, args.filesystem, args.live_map_cells)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompactError as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    except InterruptedError:
        print("Compact stopped safely between kernel-journalled transactions.", flush=True)
        raise SystemExit(EXIT_STOPPED)
