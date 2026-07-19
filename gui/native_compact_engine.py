#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Kernel-journalled free-space compaction for ext4, XFS and Btrfs.

"""Native Linux filesystem compaction engine.

The FAT, exFAT and NTFS engines edit their own on-disk metadata while the
volume is offline.  ext4, XFS and Btrfs already have kernel transaction code
for relocating mappings, so this engine mounts an otherwise unmounted volume
privately and asks the filesystem itself to perform each relocation.

ext4 and XFS use temporary, unlinked space-collector files.  The collector
occupies existing free runs.  Each low collector extent is then used directly
as the donor for a mapping exchange with the highest movable file extent.  No
second allocation is attempted after the free space has been reserved.  After
an exchange, the collector owns the old high extent and keeps it allocated
until the pass ends.  Closing the collector descriptors releases those old
high extents together, leaving free space at the physical tail.

Btrfs cannot exchange arbitrary physical file extents because every extent is
copy-on-write and back-referenced.  Its compactor therefore uses the native
online resize transaction: temporarily shrinking the filesystem forces chunks
above the new boundary into lower free chunk ranges, then the original size is
restored.  This changes physical chunk placement without invoking file defrag.
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
import sys
import tempfile
import time
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
                         moved_before_pass: int) -> ExtentPassResult:
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


def compact_extent_filesystem(device: str, filesystem: str, live_cells: int = 0) -> int:
    if filesystem == "ext4":
        block_size, device_bytes = ext4_compact_geometry(device)
    elif filesystem == "xfs":
        block_size, device_bytes = xfs_compact_geometry(device)
    else:
        raise CompactError(f"unsupported extent filesystem: {filesystem}")

    print(
        f"{filesystem.upper()} Compact will paste the highest movable regular-file extents "
        "into the lowest accessible free ranges. It does not try to reduce fragmentation.",
        flush=True,
    )
    print(
        "The engine now repeats collector passes automatically until another pass can no "
        "longer move any regular-file allocation lower.",
        flush=True,
    )

    total_moved = 0
    total_transactions = 0
    total_runtime_skipped = 0
    final_reason = ""
    passes_with_moves = 0

    with PrivateMount(device, filesystem) as mountpoint:
        for pass_number in range(1, MAX_EXTENT_COMPACT_PASSES + 1):
            if _stop_requested:
                print("Stop requested before the next Compact pass.", flush=True)
                return EXIT_STOPPED
            result = _compact_extent_pass(
                mountpoint,
                filesystem,
                block_size,
                device_bytes,
                live_cells,
                pass_number,
                total_moved,
            )
            total_moved += result.moved_bytes
            total_transactions += result.transactions
            total_runtime_skipped += result.runtime_skipped
            final_reason = result.blocked_reason

            print(
                f"{filesystem.upper()} Compact pass {pass_number} moved "
                f"{result.moved_bytes / (1024**3):.2f} GiB in "
                f"{result.transactions:,} kernel-journalled transactions.",
                flush=True,
            )

            if result.stopped:
                print(
                    f"{filesystem.upper()} Compact stopped safely after moving "
                    f"{total_moved / (1024**3):.2f} GiB in {total_transactions:,} "
                    "completed kernel-journalled transactions.",
                    flush=True,
                )
                return EXIT_STOPPED
            if result.moved_bytes <= 0:
                break

            passes_with_moves += 1
            # Closing the pass collector releases the old high source extents.
            # A new collector pass can then use the changed free-space topology
            # without requiring the user to press Compact again.
            if hasattr(os, "syncfs"):
                root_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.syncfs(root_fd)
                finally:
                    os.close(root_fd)
        else:
            final_reason = (
                f"the automatic safety limit of {MAX_EXTENT_COMPACT_PASSES} passes was reached"
            )

    print(
        f"{filesystem.upper()} Compact moved {total_moved / (1024**3):.2f} GiB in "
        f"{total_transactions:,} kernel-journalled transactions across "
        f"{passes_with_moves:,} productive passes; {total_runtime_skipped:,} additional "
        "source extents were skipped at runtime.",
        flush=True,
    )
    if final_reason:
        print(f"Compaction reached its current online boundary: {final_reason}.", flush=True)
    print(
        "All movable regular-file extents, including moves into ext4's privileged "
        "free-block reserve, have been packed as low as the filesystem's fixed metadata "
        "and directory allocations permit. Remaining allocated islands in the free tail "
        "are filesystem structures or mappings that EXT4_IOC_MOVE_EXT/XFS exchange "
        "cannot relocate.",
        flush=True,
    )
    print("100.00 percent completed", flush=True)
    return 0

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
    alignment = 256 * 1024 * 1024
    conservative_reserve = max(2 * 1024**3, largest_chunk * 2, allocated // 10)
    tighter_reserve = max(1024**3, largest_chunk, allocated // 20)
    targets: list[int] = []
    for reserve in (conservative_reserve, tighter_reserve):
        target = _align_up(1024 * 1024 + allocated + reserve, alignment)
        target = min(target, highwater - 64 * 1024 * 1024)
        if target > allocated and target < highwater:
            targets.append(target)
    # Keep order while removing duplicates.
    return list(dict.fromkeys(targets))


def compact_btrfs(device: str) -> int:
    global _active_btrfs_fd
    original_size, devid, device_size = _btrfs_super_geometry(device)
    if original_size > device_size > 0:
        raise CompactError("Btrfs filesystem size exceeds the block device")

    print(
        "Btrfs Compact uses an online shrink-and-restore transaction. Shrinking forces "
        "chunks above a temporary boundary into lower free chunk ranges; restoring the "
        "original size leaves the released space at the physical tail. File extents are "
        "not defragmented.",
        flush=True,
    )

    with PrivateMount(device, "btrfs") as mountpoint:
        fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        _active_btrfs_fd = fd
        total_reduction = 0
        cycles = 0
        restored = True
        try:
            gaps, highwater, allocated, largest_chunk, stats = _btrfs_layout(
                fd, original_size, devid
            )
            initial_highwater = highwater
            initial_gap_bytes = sum(gap.length for gap in gaps)
            if not gaps:
                print(
                    "Btrfs Compact: allocated chunks are already packed from the beginning "
                    "of the device.",
                    flush=True,
                )
                print("100.00 percent completed", flush=True)
                return 0

            print(
                f"Btrfs Compact: {len(gaps):,} internal physical chunk gaps total "
                f"{initial_gap_bytes / (1024**3):.2f} GiB below chunk high-water byte "
                f"{highwater:,}.",
                flush=True,
            )
            print(
                f"Current chunk layout was read through {stats.get('tree_search_calls', 0):,} "
                "kernel tree-search calls.",
                flush=True,
            )

            for target in _candidate_shrink_targets(allocated, largest_chunk, highwater):
                if target >= highwater:
                    continue
                if _stop_requested:
                    print("Stop requested before the next Btrfs resize transaction.", flush=True)
                    return EXIT_STOPPED
                print(
                    f"Btrfs Compact cycle {cycles + 1}: temporarily shrinking from "
                    f"{original_size / (1024**3):.2f} GiB to {target / (1024**3):.2f} GiB.",
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
                        0, sum(gap.length for gap in gaps) - sum(gap.length for gap in new_gaps)
                    )
                    total_reduction += reduction
                    cycles += 1
                    print(
                        f"Btrfs Compact cycle {cycles}: chunk high-water byte "
                        f"{highwater:,} -> {new_highwater:,}; internal chunk gaps reduced by "
                        f"{gap_reduction / (1024**3):.2f} GiB.",
                        flush=True,
                    )
                    highwater = new_highwater
                    gaps = new_gaps
                    allocated = new_allocated
                    largest_chunk = new_largest
                    stats = new_stats
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, errno.EFBIG):
                        print(
                            f"Btrfs could not reach the temporary {target / (1024**3):.2f} GiB "
                            "boundary because insufficient relocation workspace remained. "
                            "No tighter boundary will be attempted.",
                            flush=True,
                        )
                        break
                    if _stop_requested and exc.errno in (errno.EINTR, errno.ECANCELED):
                        return EXIT_STOPPED
                    raise CompactError(f"Btrfs resize failed: {os.strerror(exc.errno)}") from exc
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

                if not gaps or total_reduction <= 0:
                    break

            final_gaps, final_highwater, _final_allocated, _largest, final_stats = _btrfs_layout(
                fd, original_size, devid
            )
            final_gap_bytes = sum(gap.length for gap in final_gaps)
            actual_reduction = max(0, initial_highwater - final_highwater)
            print(
                f"Btrfs Compact completed {cycles} shrink-and-restore cycle(s). Chunk "
                f"high-water byte {initial_highwater:,} -> {final_highwater:,}, a physical "
                f"tail reduction of {actual_reduction / (1024**3):.2f} GiB.",
                flush=True,
            )
            print(
                f"Internal chunk gaps: {initial_gap_bytes / (1024**3):.2f} GiB -> "
                f"{final_gap_bytes / (1024**3):.2f} GiB. Final layout required "
                f"{final_stats.get('tree_search_calls', 0):,} kernel tree-search calls.",
                flush=True,
            )
            if actual_reduction == 0:
                print(
                    "No effective Btrfs physical compaction was achieved; the kernel could "
                    "not move the current chunk boundary lower without more workspace.",
                    flush=True,
                )
            elif final_gaps:
                print(
                    "Btrfs remains partially compacted because fixed or profile-constrained "
                    "chunk allocations still separate some free physical ranges.",
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
