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
balance ioctl, limited to one high physical chunk per transaction, and repeats
until chunk allocation no longer moves toward the beginning of the device.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import heapq
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
COLLECTOR_FLOOR = 64 * 1024 * 1024
MAX_NO_PROGRESS = 8

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
BTRFS_IOC_BALANCE_V2 = _ioc(_IOC_READ | _IOC_WRITE, 0x94, 32, 1024)
BTRFS_IOC_BALANCE_CTL = _ioc(_IOC_WRITE, 0x94, 33, 4)

BTRFS_BALANCE_CTL_CANCEL = 2
BTRFS_BALANCE_DATA = 1 << 0
BTRFS_BALANCE_SYSTEM = 1 << 1
BTRFS_BALANCE_METADATA = 1 << 2
BTRFS_BALANCE_ARGS_USAGE = 1 << 1
BTRFS_BALANCE_ARGS_DEVID = 1 << 2
BTRFS_BALANCE_ARGS_DRANGE = 1 << 3
BTRFS_BALANCE_ARGS_LIMIT = 1 << 5

_stop_requested = False
_active_btrfs_fd: int | None = None


class CompactError(RuntimeError):
    pass


class SourceNotMovable(RuntimeError):
    pass


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
            available = stats.f_bavail * stats.f_frsize
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
    _stop_requested = True
    if _active_btrfs_fd is not None:
        try:
            arg = struct.pack("=i", BTRFS_BALANCE_CTL_CANCEL)
            fcntl.ioctl(_active_btrfs_fd, BTRFS_IOC_BALANCE_CTL, arg)
        except OSError:
            pass


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


def compact_extent_filesystem(device: str, filesystem: str, live_cells: int = 0) -> int:
    del live_cells  # Reserved for future live-map updates; kept for GUI ABI stability.
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

    with PrivateMount(device, filesystem) as mountpoint:
        collector = SpaceCollector(mountpoint, block_size)
        try:
            allocated, collector_ops = collector.fill_available()
            gaps = collector.owned_extents(device_bytes)
            total_target = sum(item.length for item in gaps)
            print(
                f"Temporary space collector mapped {allocated / (1024**3):.2f} GiB of "
                f"accessible free space in {collector_ops} fallocate operations and "
                f"{len(gaps):,} physical ranges.",
                flush=True,
            )
            print(
                "Compact will exchange file mappings directly with those reserved low ranges; "
                "it will not allocate a second donor file.",
                flush=True,
            )
            if not gaps:
                print(f"{filesystem.upper()} Compact: no accessible free range requires work.", flush=True)
                return 0

            heap, file_count, skipped = scan_sources(
                mountpoint, block_size, device_bytes, collector.workspace
            )
            print(
                f"Scanned {file_count:,} regular files; {len(heap):,} movable physical extents "
                f"were found and {skipped:,} unsupported extents or files were skipped.",
                flush=True,
            )
            if not heap:
                print(f"{filesystem.upper()} Compact: no movable regular-file extents were found.", flush=True)
                return 0

            moved_bytes = 0
            transactions = 0
            gap_bytes_completed = 0
            runtime_skipped = 0
            blocked_reason = ""
            for gap in gaps:
                cursor = gap.physical
                donor_logical = gap.logical
                while cursor < gap.physical_end:
                    if _stop_requested:
                        print("Stop requested; ending Compact between kernel-journalled transactions.", flush=True)
                        return EXIT_STOPPED

                    source = _pop_high_source(heap, cursor)
                    if source is None:
                        blocked_reason = (
                            f"no movable regular-file extent remains above the free range at byte {cursor:,}"
                        )
                        break
                    available = gap.physical_end - cursor
                    move = min(available, source.length, MAX_TRANSACTION_BYTES)
                    move -= move % block_size
                    if move <= 0:
                        blocked_reason = "the next source or destination is smaller than one filesystem block"
                        break
                    source_offset = source.logical + source.length - move
                    source_physical = source.physical + source.length - move

                    try:
                        source_fd = os.open(
                            source.path,
                            os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                        )
                    except OSError:
                        source.length = 0
                        runtime_skipped += 1
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
                                _xfs_exchange(
                                    source_fd, gap.fd, source_offset, donor_logical, move
                                )
                        except SourceNotMovable as exc:
                            source.length = 0
                            runtime_skipped += 1
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
                    moved_bytes += move
                    transactions += 1
                    cursor += move
                    donor_logical += move
                    gap_bytes_completed += move
                    percent = min(100.0, 100.0 * gap_bytes_completed / max(1, total_target))
                    print(
                        f"{filesystem.upper()} Compact: pasted {move / (1024**2):.1f} MiB "
                        f"from physical byte {source_physical:,} into byte {cursor - move:,}; "
                        f"total {moved_bytes / (1024**3):.2f} GiB.",
                        flush=True,
                    )
                    print(f"{percent:.2f} percent completed", flush=True)
                if blocked_reason:
                    break

            if hasattr(os, "syncfs"):
                root_fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.syncfs(root_fd)
                finally:
                    os.close(root_fd)
            print(
                f"{filesystem.upper()} Compact moved {moved_bytes / (1024**3):.2f} GiB "
                f"in {transactions:,} kernel-journalled transactions; "
                f"{runtime_skipped:,} additional source extents were skipped at runtime.",
                flush=True,
            )
            if blocked_reason:
                print(f"Compaction stopped at a conservative boundary: {blocked_reason}.", flush=True)
            else:
                print("Every accessible low free range that could be filled with higher file data was processed.", flush=True)
            return 0
        finally:
            collector.close()


def _btrfs_layout(device: str) -> tuple[int, int, list[FreeRange], int, int]:
    with Reader(device) as reader:
        sb = reader.read(btrfs_backend._SUPER_OFFSET, btrfs_backend._SUPER_SIZE)
        if sb[0x40:0x48] != btrfs_backend._BTRFS_MAGIC:
            raise CompactError("not a Btrfs filesystem")
        total = u64le(sb, 112)
        num_devices = u64le(sb, 136)
        nodesize = u32le(sb, 148)
        chunk_root = u64le(sb, 88)
        chunk_level = sb[199]
        devid = u64le(sb, 201)
        if num_devices != 1:
            raise CompactError("Btrfs Compact currently supports single-device filesystems only")
        chunks = btrfs_backend._system_chunks(sb)
        bootstrap = btrfs_backend._Mapper(chunks, devid, total)
        tree = btrfs_backend._TreeReader(reader, bootstrap, nodesize)
        items, _blocks = tree.walk(chunk_root, chunk_level)
        for item in items:
            if item.key.type == btrfs_backend._CHUNK_ITEM:
                chunks.append(btrfs_backend._parse_chunk(item.data, item.key.offset))
        mapper = btrfs_backend._Mapper(chunks, devid, total)
        ranges: list[tuple[int, int]] = []
        for chunk in mapper.chunks:
            for stripe_devid, physical in chunk.stripes:
                if stripe_devid == devid:
                    ranges.append((physical, physical + chunk.length))
        allocated = _merge_ranges(ranges)
        gaps: list[FreeRange] = []
        cursor = 1024 * 1024
        for item in allocated:
            start = max(cursor, item.start)
            if start > cursor:
                gaps.append(FreeRange(cursor, start))
            cursor = max(cursor, item.end)
        highwater = max((item.end for item in allocated), default=cursor)
        return total, devid, gaps, highwater, sum(item.length for item in allocated)


def _balance_request(devid: int, start: int, end: int) -> bytearray:
    request = bytearray(1024)
    top_flags = BTRFS_BALANCE_DATA | BTRFS_BALANCE_METADATA | BTRFS_BALANCE_SYSTEM
    struct.pack_into("=Q", request, 0, top_flags)
    section_flags = BTRFS_BALANCE_ARGS_DEVID | BTRFS_BALANCE_ARGS_DRANGE | BTRFS_BALANCE_ARGS_LIMIT
    for offset in (16, 152, 288):
        struct.pack_into("=Q", request, offset + 16, devid)
        struct.pack_into("=Q", request, offset + 24, start)
        struct.pack_into("=Q", request, offset + 32, end)
        struct.pack_into("=Q", request, offset + 64, section_flags)
        struct.pack_into("=Q", request, offset + 72, 1)
    return request


def compact_btrfs(device: str) -> int:
    global _active_btrfs_fd
    total, devid, gaps, highwater, allocated = _btrfs_layout(device)
    if not gaps:
        print("Btrfs Compact: allocated chunks are already packed from the beginning of the device.", flush=True)
        return 0
    print(
        f"Btrfs Compact: {len(gaps):,} physical gaps exist below the chunk high-water mark "
        f"at byte {highwater:,}.", flush=True
    )
    print(
        "Btrfs uses copy-on-write back-referenced extents, so Compact asks the native Btrfs "
        "balance transaction engine to relocate one high physical chunk at a time. It does "
        "not invoke file defragmentation.", flush=True
    )
    with PrivateMount(device, "btrfs") as mountpoint:
        fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        _active_btrfs_fd = fd
        try:
            initial_gap_bytes = sum(gap.length for gap in gaps)
            moved_chunks = 0
            no_progress = 0
            previous_gap_bytes = initial_gap_bytes
            previous_gap_start = gaps[0].start
            previous_highwater = highwater
            while gaps and no_progress < MAX_NO_PROGRESS:
                if _stop_requested:
                    print("Stop requested; cancelling the active Btrfs balance transaction.", flush=True)
                    return EXIT_STOPPED
                target = gaps[0]
                request = _balance_request(devid, target.start, total)
                try:
                    fcntl.ioctl(fd, BTRFS_IOC_BALANCE_V2, request, True)
                except OSError as exc:
                    if _stop_requested or exc.errno in (errno.ECANCELED, errno.EINTR):
                        return EXIT_STOPPED
                    if exc.errno in (errno.ENOSPC, errno.EBUSY):
                        print(
                            f"Btrfs balance could not relocate another high chunk: {os.strerror(exc.errno)}.",
                            flush=True,
                        )
                        break
                    raise
                moved_chunks += 1
                if hasattr(os, "syncfs"):
                    os.syncfs(fd)
                total, devid, gaps, highwater, allocated = _btrfs_layout(device)
                remaining = sum(gap.length for gap in gaps)
                next_gap_start = gaps[0].start if gaps else highwater
                progressed = (
                    remaining < previous_gap_bytes
                    or next_gap_start > previous_gap_start
                    or highwater < previous_highwater
                )
                no_progress = 0 if progressed else no_progress + 1
                previous_gap_bytes = remaining
                previous_gap_start = next_gap_start
                previous_highwater = highwater
                reduced = max(0, initial_gap_bytes - remaining)
                percent = min(100.0, 100.0 * reduced / max(1, initial_gap_bytes))
                print(
                    f"Btrfs Compact: completed {moved_chunks} balance transaction(s); "
                    f"chunk high-water byte {highwater:,}; internal chunk gaps "
                    f"{remaining / (1024**3):.2f} GiB.", flush=True
                )
                print(f"{percent:.2f} percent completed", flush=True)
            if not gaps:
                print("Btrfs Compact packed all allocated chunks into one physical prefix.", flush=True)
            elif no_progress >= MAX_NO_PROGRESS:
                print(
                    "Btrfs Compact stopped because the allocator made no further physical progress "
                    f"for {MAX_NO_PROGRESS} balance transactions.", flush=True
                )
            else:
                print("Btrfs Compact completed the available native balance work.", flush=True)
            return 0
        finally:
            _active_btrfs_fd = None
            os.close(fd)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Native kernel-journalled free-space compaction for ext4, XFS and Btrfs"
    )
    parser.add_argument("operation", choices=("compact",))
    parser.add_argument("device")
    parser.add_argument("--filesystem", required=True, choices=("ext4", "xfs", "btrfs"))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--journal")
    parser.add_argument("--ram-buffer", default="auto")
    parser.add_argument("--workers", default="auto")
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
