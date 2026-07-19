#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Native offline NTFS compaction and interrupted-transaction recovery.

"""Conservative native NTFS compaction.

The engine relocates ordinary, uncompressed, non-sparse, non-encrypted unnamed
file data streams from high clusters into lower free contiguous runs.  It edits
only the stream's mapping pairs, the volume $Bitmap and the affected MFT record.
System files, directories, attribute-list streams and layouts that do not fit
back into their existing MFT attribute are deliberately left untouched.

Each stream move is a separate externally journalled transaction.  Destination
clusters are copied first, then reserved in $Bitmap, then the MFT mapping pairs
are switched, and finally the old clusters are released.  Recovery is
idempotent and inspects the on-disk MFT record so it can finish or roll back even
when a power loss occurred between a metadata write and its journal update.
"""

from __future__ import annotations

import argparse
import base64
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
from typing import Iterable, Iterator, TextIO

SCHEMA = 2
JOURNAL_KIND = "linux-defragger-native-ntfs-move"
BLKGETSIZE64 = 0x80081272
COPY_CHUNK = 8 * 1024 * 1024
MFT_RECORD_CHUNK = 16 * 1024 * 1024
FIRST_USER_RECORD = 24
REPORT_EVERY_FILES = 128
REPORT_EVERY_CLUSTERS = 262144
REPORT_EVERY_SECONDS = 2.0

ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_VOLUME_INFORMATION = 0x70
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_BITMAP = 0xB0
RECORD_IN_USE = 0x0001
RECORD_DIRECTORY = 0x0002
ATTR_COMPRESSED = 0x0001
ATTR_ENCRYPTED = 0x4000
ATTR_SPARSE = 0x8000
FILE_REFERENCE_MASK = (1 << 48) - 1

# $VOLUME_INFORMATION flags.  Only VOLUME_IS_DIRTY means the filesystem is
# dirty.  Bit 0x0080 is present on some valid modern volumes but remains
# undocumented; preserve it exactly rather than treating every non-zero flag
# as corruption.  Unknown flags other than this observed preserved bit are
# rejected conservatively.
VOLUME_IS_DIRTY = 0x0001
VOLUME_RESIZE_LOG_FILE = 0x0002
VOLUME_UPGRADE_ON_MOUNT = 0x0004
VOLUME_MOUNTED_ON_NT4 = 0x0008
VOLUME_DELETE_USN_UNDERWAY = 0x0010
VOLUME_REPAIR_OBJECT_ID = 0x0020
VOLUME_OBSERVED_UNKNOWN_0080 = 0x0080
VOLUME_CHKDSK_UNDERWAY = 0x4000
VOLUME_MODIFIED_BY_CHKDSK = 0x8000
VOLUME_UNSAFE_WRITE_MASK = (
    VOLUME_IS_DIRTY | VOLUME_RESIZE_LOG_FILE | VOLUME_UPGRADE_ON_MOUNT |
    VOLUME_DELETE_USN_UNDERWAY | VOLUME_REPAIR_OBJECT_ID |
    VOLUME_CHKDSK_UNDERWAY | VOLUME_MODIFIED_BY_CHKDSK
)
VOLUME_PRESERVED_SAFE_MASK = VOLUME_MOUNTED_ON_NT4 | VOLUME_OBSERVED_UNKNOWN_0080
VOLUME_ACCEPTED_MASK = VOLUME_UNSAFE_WRITE_MASK | VOLUME_PRESERVED_SAFE_MASK

_stop_requested = False


class NtfsCompactError(RuntimeError):
    pass


@dataclass(frozen=True)
class Run:
    lcn: int | None
    length: int


@dataclass(frozen=True)
class Volume:
    path: str
    fd: int
    device_size: int
    bytes_per_sector: int
    sectors_per_cluster: int
    cluster_size: int
    total_clusters: int
    mft_lcn: int
    mftmirr_lcn: int
    mft_record_size: int
    serial: str


@dataclass(frozen=True)
class Attribute:
    offset: int
    length: int
    atype: int
    name: bytes
    flags: int
    nonresident: bool
    lowest_vcn: int = 0
    highest_vcn: int = 0
    run_offset: int = 0
    runs: tuple[Run, ...] = ()
    data_size: int = 0
    allocated_size: int = 0
    initialized_size: int = 0


@dataclass(frozen=True)
class Candidate:
    record_number: int
    record_offset: int
    record_raw: bytes
    record_fixed: bytes
    attribute: Attribute

    @property
    def clusters(self) -> int:
        return sum(run.length for run in self.attribute.runs if run.lcn is not None)

    @property
    def highest_lcn(self) -> int:
        return max((run.lcn + run.length for run in self.attribute.runs if run.lcn is not None), default=0)

    @property
    def lowest_lcn(self) -> int:
        return min((run.lcn for run in self.attribute.runs if run.lcn is not None), default=0)


@dataclass(frozen=True)
class NtfsLayout:
    volume: Volume
    mft_runs: tuple[Run, ...]
    mft_data_size: int
    bitmap_runs: tuple[Run, ...]
    bitmap_data_size: int
    bitmap: bytearray


@dataclass(frozen=True)
class StreamInfo:
    record_number: int
    base_record_number: int
    attribute_offset: int
    attribute_type: int
    attribute_name: str
    file_name: str
    flags: int
    runs: tuple[Run, ...]
    movable: bool
    blocker_reason: str
    generation: int = 0

    @property
    def key(self) -> tuple[int, int]:
        return self.record_number, self.attribute_offset

    @property
    def clusters(self) -> int:
        return sum(run.length for run in self.runs if run.lcn is not None)

    @property
    def highest_lcn(self) -> int:
        return max((run.lcn + run.length for run in self.runs if run.lcn is not None), default=0)

    @property
    def lowest_lcn(self) -> int:
        return min((run.lcn for run in self.runs if run.lcn is not None), default=0)


@dataclass
class AllocationPlan:
    streams: dict[tuple[int, int], StreamInfo]
    heap: list[tuple[int, int, int, int]]
    movable_count: int
    malformed_records: int
    hibernation_active: bool


SYSTEM_RECORD_NAMES = {
    0: "$MFT", 1: "$MFTMirr", 2: "$LogFile", 3: "$Volume",
    4: "$AttrDef", 5: "$Root", 6: "$Bitmap", 7: "$Boot",
    8: "$BadClus", 9: "$Secure", 10: "$UpCase", 11: "$Extend",
}

ATTRIBUTE_NAMES = {
    0x10: "$STANDARD_INFORMATION", ATTR_ATTRIBUTE_LIST: "$ATTRIBUTE_LIST",
    ATTR_FILE_NAME: "$FILE_NAME", 0x40: "$OBJECT_ID", 0x50: "$SECURITY_DESCRIPTOR",
    ATTR_VOLUME_INFORMATION: "$VOLUME_INFORMATION", 0x60: "$VOLUME_NAME",
    ATTR_DATA: "$DATA", ATTR_INDEX_ROOT: "$INDEX_ROOT",
    ATTR_INDEX_ALLOCATION: "$INDEX_ALLOCATION", ATTR_BITMAP: "$BITMAP",
    0xC0: "$REPARSE_POINT", 0xD0: "$EA_INFORMATION", 0xE0: "$EA",
    0xF0: "$PROPERTY_SET", 0x100: "$LOGGED_UTILITY_STREAM",
}


def _stop(_signum: int, _frame: object) -> None:
    global _stop_requested
    _stop_requested = True
    print("Stop requested; the active NTFS stream transaction will finish safely.", flush=True)


def _u16(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _device_size(path: str) -> int:
    st = os.stat(path)
    if stat.S_ISREG(st.st_mode):
        return st.st_size
    if not stat.S_ISBLK(st.st_mode):
        raise NtfsCompactError("target is neither a regular image nor a block device")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        raw = fcntl.ioctl(fd, BLKGETSIZE64, b"\0" * 8)
        return struct.unpack("Q", raw)[0]
    finally:
        os.close(fd)


def _is_mounted(path: str) -> bool:
    st = os.stat(path)
    if not stat.S_ISBLK(st.st_mode):
        return False
    dev = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as stream:
        return any(len(fields := line.split()) > 2 and fields[2] == dev for line in stream)


def _open_volume(path: str, write: bool) -> Volume:
    realpath = os.path.realpath(path)
    flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(realpath, flags)
    try:
        if write:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise NtfsCompactError(f"cannot lock NTFS target exclusively: {exc}") from exc
        boot = os.pread(fd, 512, 0)
        if len(boot) != 512 or boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa":
            raise NtfsCompactError("target does not contain a valid NTFS boot sector")
        bps = _u16(boot, 11)
        spc = boot[13]
        if bps < 256 or bps > 4096 or bps & (bps - 1):
            raise NtfsCompactError("invalid NTFS bytes-per-sector value")
        if spc == 0 or spc & (spc - 1):
            raise NtfsCompactError("invalid NTFS sectors-per-cluster value")
        cluster_size = bps * spc
        if cluster_size > 64 * 1024:
            raise NtfsCompactError("unsupported NTFS cluster size")
        total_sectors = _u64(boot, 40)
        total_clusters = total_sectors // spc
        device_size = _device_size(realpath)
        if total_sectors * bps > device_size:
            raise NtfsCompactError("NTFS volume boundary exceeds the target device")
        mft_lcn = _u64(boot, 48)
        mftmirr_lcn = _u64(boot, 56)
        rec_raw = int.from_bytes(boot[64:65], "little", signed=True)
        record_size = (1 << -rec_raw) if rec_raw < 0 else rec_raw * cluster_size
        if record_size < 512 or record_size > 1024 * 1024 or record_size % bps:
            raise NtfsCompactError("invalid NTFS MFT record size")
        return Volume(
            path=realpath,
            fd=fd,
            device_size=device_size,
            bytes_per_sector=bps,
            sectors_per_cluster=spc,
            cluster_size=cluster_size,
            total_clusters=total_clusters,
            mft_lcn=mft_lcn,
            mftmirr_lcn=mftmirr_lcn,
            mft_record_size=record_size,
            serial=boot[72:80].hex(),
        )
    except Exception:
        os.close(fd)
        raise


def _close_volume(volume: Volume) -> None:
    os.close(volume.fd)


def _pread_exact(fd: int, length: int, offset: int) -> bytes:
    data = os.pread(fd, length, offset)
    if len(data) != length:
        raise NtfsCompactError(f"short read at byte {offset}: wanted {length}, got {len(data)}")
    return data


def _pwrite_exact(fd: int, data: bytes, offset: int) -> None:
    done = 0
    while done < len(data):
        written = os.pwrite(fd, data[done:], offset + done)
        if written <= 0:
            raise NtfsCompactError(f"short write at byte {offset + done}")
        done += written


def _signed_min_bytes(value: int) -> bytes:
    for size in range(1, 9):
        try:
            encoded = value.to_bytes(size, "little", signed=True)
        except OverflowError:
            continue
        if int.from_bytes(encoded, "little", signed=True) == value:
            return encoded
    raise NtfsCompactError("NTFS runlist delta exceeds 64-bit encoding")


def _unsigned_min_bytes(value: int) -> bytes:
    if value <= 0:
        raise NtfsCompactError("NTFS run length must be positive")
    size = max(1, (value.bit_length() + 7) // 8)
    if size > 8:
        raise NtfsCompactError("NTFS run length exceeds 64-bit encoding")
    return value.to_bytes(size, "little")


def _decode_runlist(data: bytes) -> tuple[Run, ...]:
    pos = 0
    previous_lcn = 0
    result: list[Run] = []
    while pos < len(data):
        header = data[pos]
        pos += 1
        if header == 0:
            return tuple(result)
        length_size = header & 0x0F
        offset_size = header >> 4
        if not length_size or length_size > 8 or offset_size > 8 or pos + length_size + offset_size > len(data):
            raise NtfsCompactError("invalid NTFS mapping-pairs array")
        length = int.from_bytes(data[pos:pos + length_size], "little")
        pos += length_size
        if length <= 0:
            raise NtfsCompactError("invalid zero-length NTFS run")
        if offset_size == 0:
            lcn = None
        else:
            delta = int.from_bytes(data[pos:pos + offset_size], "little", signed=True)
            pos += offset_size
            previous_lcn += delta
            if previous_lcn < 0:
                raise NtfsCompactError("invalid negative NTFS logical cluster number")
            lcn = previous_lcn
        result.append(Run(lcn, length))
    raise NtfsCompactError("unterminated NTFS mapping-pairs array")


def _encode_runlist(runs: Iterable[Run]) -> bytes:
    previous_lcn = 0
    output = bytearray()
    for run in runs:
        length_bytes = _unsigned_min_bytes(run.length)
        if run.lcn is None:
            offset_bytes = b""
        else:
            offset_bytes = _signed_min_bytes(run.lcn - previous_lcn)
            previous_lcn = run.lcn
        output.append((len(offset_bytes) << 4) | len(length_bytes))
        output += length_bytes
        output += offset_bytes
    output.append(0)
    return bytes(output)


def _apply_fixups(raw: bytes, sector_size: int) -> bytearray:
    record = bytearray(raw)
    usa_off = _u16(record, 4)
    usa_count = _u16(record, 6)
    expected_count = len(record) // sector_size + 1
    if usa_count != expected_count or usa_off + usa_count * 2 > len(record):
        raise NtfsCompactError("invalid NTFS update-sequence array")
    usn = bytes(record[usa_off:usa_off + 2])
    for index in range(1, usa_count):
        end = index * sector_size
        if bytes(record[end - 2:end]) != usn:
            raise NtfsCompactError("NTFS MFT update-sequence mismatch")
        record[end - 2:end] = record[usa_off + index * 2:usa_off + index * 2 + 2]
    return record


def _prepare_fixups(fixed: bytes | bytearray, sector_size: int) -> bytes:
    record = bytearray(fixed)
    usa_off = _u16(record, 4)
    usa_count = _u16(record, 6)
    expected_count = len(record) // sector_size + 1
    if usa_count != expected_count or usa_off + usa_count * 2 > len(record):
        raise NtfsCompactError("invalid NTFS update-sequence array")
    old = _u16(record, usa_off)
    new = (old + 1) & 0xFFFF
    if new == 0:
        new = 1
    usn = struct.pack("<H", new)
    record[usa_off:usa_off + 2] = usn
    for index in range(1, usa_count):
        end = index * sector_size
        record[usa_off + index * 2:usa_off + index * 2 + 2] = record[end - 2:end]
        record[end - 2:end] = usn
    return bytes(record)


def _attributes(record: bytes | bytearray) -> Iterator[Attribute]:
    pos = _u16(record, 20)
    in_use = min(len(record), _u32(record, 24))
    while pos + 16 <= in_use:
        atype = _u32(record, pos)
        if atype == 0xFFFFFFFF:
            return
        length = _u32(record, pos + 4)
        if length < 24 or pos + length > in_use:
            raise NtfsCompactError("invalid NTFS attribute record")
        nonresident = bool(record[pos + 8])
        name_len = record[pos + 9]
        name_off = _u16(record, pos + 10)
        flags = _u16(record, pos + 12)
        if name_len:
            name_end = name_off + name_len * 2
            if name_off < 16 or name_end > length:
                raise NtfsCompactError("invalid NTFS attribute name")
            name = bytes(record[pos + name_off:pos + name_end])
        else:
            name = b""
        if nonresident:
            if length < 64:
                raise NtfsCompactError("truncated NTFS non-resident attribute")
            lowest_vcn = _u64(record, pos + 16)
            highest_vcn = _u64(record, pos + 24)
            run_offset = _u16(record, pos + 32)
            if run_offset < 64 or run_offset >= length:
                raise NtfsCompactError("invalid NTFS mapping-pairs offset")
            runs = _decode_runlist(bytes(record[pos + run_offset:pos + length]))
            expected = highest_vcn - lowest_vcn + 1
            if sum(run.length for run in runs) != expected:
                raise NtfsCompactError("NTFS runlist length does not match its VCN range")
            yield Attribute(
                offset=pos,
                length=length,
                atype=atype,
                name=name,
                flags=flags,
                nonresident=True,
                lowest_vcn=lowest_vcn,
                highest_vcn=highest_vcn,
                run_offset=run_offset,
                runs=runs,
                allocated_size=_u64(record, pos + 40),
                data_size=_u64(record, pos + 48),
                initialized_size=_u64(record, pos + 56),
            )
        else:
            yield Attribute(pos, length, atype, name, flags, False)
        pos += length


def _stream_segments(runs: Iterable[Run], cluster_size: int, logical_offset: int, length: int) -> Iterator[tuple[int, int]]:
    if logical_offset < 0 or length < 0:
        raise NtfsCompactError("negative NTFS stream range")
    cursor = 0
    remaining = length
    wanted = logical_offset
    for run in runs:
        run_bytes = run.length * cluster_size
        if wanted >= cursor + run_bytes:
            cursor += run_bytes
            continue
        within = max(0, wanted - cursor)
        take = min(remaining, run_bytes - within)
        if run.lcn is None:
            raise NtfsCompactError("sparse run encountered in a metadata stream")
        yield run.lcn * cluster_size + within, take
        remaining -= take
        wanted += take
        cursor += run_bytes
        if remaining == 0:
            return
    if remaining:
        raise NtfsCompactError("NTFS stream runlist is shorter than requested range")


def _read_stream(volume: Volume, runs: Iterable[Run], logical_offset: int, length: int) -> bytes:
    result = bytearray()
    for physical, take in _stream_segments(runs, volume.cluster_size, logical_offset, length):
        result += _pread_exact(volume.fd, take, physical)
    return bytes(result)


def _write_stream(volume: Volume, runs: Iterable[Run], logical_offset: int, data: bytes) -> None:
    consumed = 0
    for physical, take in _stream_segments(runs, volume.cluster_size, logical_offset, len(data)):
        _pwrite_exact(volume.fd, data[consumed:consumed + take], physical)
        consumed += take
    if consumed != len(data):
        raise NtfsCompactError("short NTFS metadata-stream write")


def _record_zero(volume: Volume) -> tuple[bytes, bytearray, Attribute]:
    raw = _pread_exact(volume.fd, volume.mft_record_size, volume.mft_lcn * volume.cluster_size)
    if raw[:4] != b"FILE":
        raise NtfsCompactError("NTFS $MFT record zero was not found")
    fixed = _apply_fixups(raw, volume.bytes_per_sector)
    data_attrs = [attr for attr in _attributes(fixed)
                  if attr.atype == ATTR_DATA and not attr.name and attr.nonresident]
    if len(data_attrs) != 1 or data_attrs[0].lowest_vcn != 0:
        raise NtfsCompactError("unsupported split $MFT data attribute")
    return raw, fixed, data_attrs[0]


def _read_mft_record(volume: Volume, mft_runs: Iterable[Run], record_number: int) -> tuple[int, bytes, bytearray]:
    logical = record_number * volume.mft_record_size
    raw = _read_stream(volume, mft_runs, logical, volume.mft_record_size)
    fixed = _apply_fixups(raw, volume.bytes_per_sector)
    first_segment = next(_stream_segments(mft_runs, volume.cluster_size, logical, 1))
    return first_segment[0], raw, fixed


def _write_mft_record(volume: Volume, mft_runs: Iterable[Run], record_number: int, raw: bytes) -> None:
    _write_stream(volume, mft_runs, record_number * volume.mft_record_size, raw)


def _validate_volume_flags(flags: int, *, allow_dirty: bool = False) -> None:
    """Reject unsafe NTFS states while preserving benign non-zero flags.

    The old implementation treated any non-zero value as "dirty".  NTFS uses
    only bit 0x0001 for that state.  We retain benign flags verbatim when
    temporarily adding our transaction dirty bit.
    """
    unsupported = flags & ~VOLUME_ACCEPTED_MASK
    if unsupported:
        raise NtfsCompactError(
            f"unsupported NTFS volume flags are set (0x{flags:04x}; "
            f"unknown mask 0x{unsupported:04x})"
        )
    if (flags & VOLUME_IS_DIRTY) and not allow_dirty:
        raise NtfsCompactError(
            f"NTFS dirty flag is set (0x{flags:04x}); run Windows chkdsk first"
        )
    unsafe = flags & VOLUME_UNSAFE_WRITE_MASK
    if allow_dirty:
        unsafe &= ~VOLUME_IS_DIRTY
    if unsafe:
        raise NtfsCompactError(
            f"NTFS volume has an active maintenance state (0x{flags:04x}); "
            "complete it in Windows before compacting"
        )


def _read_layout(volume: Volume, *, allow_dirty: bool = False, check_volume: bool = True) -> NtfsLayout:
    _raw0, _fixed0, mft_attr = _record_zero(volume)
    mft_runs = mft_attr.runs
    if check_volume:
        _, raw_volume, fixed_volume = _read_mft_record(volume, mft_runs, 3)
        del raw_volume
        flags_seen = None
        for attr in _attributes(fixed_volume):
            if attr.atype == ATTR_VOLUME_INFORMATION and not attr.nonresident:
                value_len = _u32(fixed_volume, attr.offset + 16)
                value_off = _u16(fixed_volume, attr.offset + 20)
                if value_len < 12 or value_off + value_len > attr.length:
                    raise NtfsCompactError("invalid NTFS volume-information attribute")
                flags_seen = _u16(fixed_volume, attr.offset + value_off + 10)
                break
        if flags_seen is None:
            raise NtfsCompactError("NTFS volume-information attribute was not found")
        _validate_volume_flags(flags_seen, allow_dirty=allow_dirty)

    _, _raw6, fixed6 = _read_mft_record(volume, mft_runs, 6)
    bitmap_attrs = [attr for attr in _attributes(fixed6)
                    if attr.atype == ATTR_DATA and not attr.name and attr.nonresident]
    if len(bitmap_attrs) != 1 or bitmap_attrs[0].lowest_vcn != 0:
        raise NtfsCompactError("unsupported split or resident NTFS $Bitmap stream")
    bitmap_attr = bitmap_attrs[0]
    bitmap_data = bytearray(_read_stream(volume, bitmap_attr.runs, 0, bitmap_attr.data_size))
    if len(bitmap_data) * 8 < volume.total_clusters:
        raise NtfsCompactError("NTFS $Bitmap is shorter than the volume")
    return NtfsLayout(volume, tuple(mft_runs), mft_attr.data_size,
                      tuple(bitmap_attr.runs), bitmap_attr.data_size, bitmap_data)


def _volume_record_state(layout: NtfsLayout) -> tuple[bytes, bytes, bytes, bytes]:
    """Return clean and dirty raw images for $Volume and its MFT mirror."""
    volume = layout.volume
    _offset, raw, fixed = _read_mft_record(volume, layout.mft_runs, 3)
    info = None
    for attr in _attributes(fixed):
        if attr.atype == ATTR_VOLUME_INFORMATION and not attr.nonresident:
            info = attr
            break
    if info is None:
        raise NtfsCompactError("NTFS volume-information attribute was not found")
    value_len = _u32(fixed, info.offset + 16)
    value_off = _u16(fixed, info.offset + 20)
    if value_len < 12 or value_off + value_len > info.length:
        raise NtfsCompactError("invalid NTFS volume-information attribute")
    flags_offset = info.offset + value_off + 10
    flags = _u16(fixed, flags_offset)
    _validate_volume_flags(flags)
    dirty_fixed = bytearray(fixed)
    struct.pack_into("<H", dirty_fixed, flags_offset, flags | VOLUME_IS_DIRTY)
    dirty_raw = _prepare_fixups(dirty_fixed, volume.bytes_per_sector)

    mirror_offset = volume.mftmirr_lcn * volume.cluster_size + 3 * volume.mft_record_size
    mirror_raw = _pread_exact(volume.fd, volume.mft_record_size, mirror_offset)
    mirror_fixed = _apply_fixups(mirror_raw, volume.bytes_per_sector)
    mirror_info = None
    for attr in _attributes(mirror_fixed):
        if attr.atype == ATTR_VOLUME_INFORMATION and not attr.nonresident:
            mirror_info = attr
            break
    if mirror_info is None:
        raise NtfsCompactError("NTFS $MFTMirr does not contain a valid $Volume record")
    mirror_value_off = _u16(mirror_fixed, mirror_info.offset + 20)
    mirror_flags_offset = mirror_info.offset + mirror_value_off + 10
    if _u16(mirror_fixed, mirror_flags_offset) != flags:
        raise NtfsCompactError("NTFS $Volume and $MFTMirr flags disagree")
    mirror_dirty_fixed = bytearray(mirror_fixed)
    struct.pack_into("<H", mirror_dirty_fixed, mirror_flags_offset, flags | VOLUME_IS_DIRTY)
    mirror_dirty_raw = _prepare_fixups(mirror_dirty_fixed, volume.bytes_per_sector)
    return raw, dirty_raw, mirror_raw, mirror_dirty_raw


def _write_volume_records(layout: NtfsLayout, mft_raw: bytes, mirror_raw: bytes) -> None:
    volume = layout.volume
    _write_mft_record(volume, layout.mft_runs, 3, mft_raw)
    mirror_offset = volume.mftmirr_lcn * volume.cluster_size + 3 * volume.mft_record_size
    _pwrite_exact(volume.fd, mirror_raw, mirror_offset)


def _resident_value(record: bytes | bytearray, attr: Attribute) -> bytes:
    if attr.nonresident:
        raise NtfsCompactError("attribute is not resident")
    value_len = _u32(record, attr.offset + 16)
    value_off = _u16(record, attr.offset + 20)
    if value_off < 24 or value_off + value_len > attr.length:
        raise NtfsCompactError("invalid resident NTFS attribute value")
    return bytes(record[attr.offset + value_off:attr.offset + value_off + value_len])


def _hibernation_active(layout: NtfsLayout) -> bool:
    """Detect an active Windows hibernation image without mounting the volume."""
    volume = layout.volume
    record_count = layout.mft_data_size // volume.mft_record_size
    for number in range(FIRST_USER_RECORD, record_count):
        try:
            _offset, _raw, fixed = _read_mft_record(volume, layout.mft_runs, number)
        except NtfsCompactError:
            continue
        if fixed[:4] != b"FILE" or not (_u16(fixed, 22) & RECORD_IN_USE):
            continue
        try:
            attrs = list(_attributes(fixed))
        except NtfsCompactError:
            continue
        found = False
        for attr in attrs:
            if attr.atype != 0x30 or attr.nonresident:
                continue
            value = _resident_value(fixed, attr)
            if len(value) < 66:
                continue
            name_len = value[64]
            name = value[66:66 + name_len * 2].decode("utf-16le", errors="ignore")
            if name.casefold() == "hiberfil.sys":
                found = True
                break
        if not found:
            continue
        data_attrs = [attr for attr in attrs if attr.atype == ATTR_DATA and not attr.name]
        if not data_attrs:
            return False
        attr = data_attrs[0]
        if attr.nonresident:
            if not attr.runs or attr.data_size == 0:
                return False
            header = _read_stream(volume, attr.runs, 0, min(4096, attr.data_size))
        else:
            header = _resident_value(fixed, attr)
        return bool(header[:4].strip(b"\0"))
    return False


def _bit(bitmap: bytes | bytearray, cluster: int) -> bool:
    return bool(bitmap[cluster >> 3] & (1 << (cluster & 7)))


def _set_bit(bitmap: bytearray, cluster: int, value: bool) -> None:
    mask = 1 << (cluster & 7)
    index = cluster >> 3
    if value:
        bitmap[index] |= mask
    else:
        bitmap[index] &= ~mask & 0xFF


def _set_range(bitmap: bytearray, start: int, length: int, value: bool) -> None:
    for cluster in range(start, start + length):
        _set_bit(bitmap, cluster, value)


def _bitmap_patches(layout: NtfsLayout, clusters: Iterable[int]) -> list[tuple[int, bytes]]:
    """Return compact snapshots of only the bitmap bytes a move may change."""
    indices = sorted({cluster >> 3 for cluster in clusters})
    if not indices:
        return []
    patches: list[tuple[int, bytes]] = []
    run_start = previous = indices[0]
    for index in indices[1:]:
        if index != previous + 1:
            patches.append((run_start, bytes(layout.bitmap[run_start:previous + 1])))
            run_start = index
        previous = index
    patches.append((run_start, bytes(layout.bitmap[run_start:previous + 1])))
    return patches


def _write_bitmap_bytes(layout: NtfsLayout, byte_offset: int, data: bytes) -> None:
    _write_stream(layout.volume, layout.bitmap_runs, byte_offset, data)


def _write_bitmap_patches(layout: NtfsLayout, patches: Iterable[tuple[int, bytes]]) -> None:
    for offset, data in patches:
        _write_bitmap_bytes(layout, offset, data)


def _current_bitmap_patches(layout: NtfsLayout, snapshots: Iterable[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
    return [(offset, bytes(layout.bitmap[offset:offset + len(original)]))
            for offset, original in snapshots]

def _highest_used(bitmap: bytes | bytearray, total_clusters: int) -> int:
    for cluster in range(total_clusters - 1, -1, -1):
        if _bit(bitmap, cluster):
            return cluster + 1
    return 0


def _find_free_run(bitmap: bytes | bytearray, length: int, before: int) -> int | None:
    if length <= 0:
        return None
    run_start = -1
    run_length = 0
    for cluster in range(max(0, min(before, len(bitmap) * 8))):
        if not _bit(bitmap, cluster):
            if run_length == 0:
                run_start = cluster
            run_length += 1
            if run_length >= length:
                return run_start
        else:
            run_start = -1
            run_length = 0
    return None


def _decode_attribute_name(name: bytes) -> str:
    if not name:
        return ""
    return name.decode("utf-16le", errors="replace")


def _best_file_name(record: bytes | bytearray, attrs: Iterable[Attribute], record_number: int) -> str:
    if record_number in SYSTEM_RECORD_NAMES:
        return SYSTEM_RECORD_NAMES[record_number]
    choices: list[tuple[int, str]] = []
    for attr in attrs:
        if attr.atype != ATTR_FILE_NAME or attr.nonresident:
            continue
        try:
            value = _resident_value(record, attr)
        except NtfsCompactError:
            continue
        if len(value) < 66:
            continue
        name_length = value[64]
        namespace = value[65]
        end = 66 + name_length * 2
        if end > len(value):
            continue
        name = value[66:end].decode("utf-16le", errors="replace")
        if not name:
            continue
        # Prefer Win32 and Win32+DOS names over POSIX, and DOS aliases last.
        score = 3 if namespace in (1, 3) else 2 if namespace == 0 else 1
        choices.append((score, name))
    return max(choices, default=(0, ""))[1]


def _select_movable_attribute(record_number: int, fixed: bytes | bytearray,
                              attrs: list[Attribute]) -> Attribute | None:
    flags = _u16(fixed, 22)
    if record_number < FIRST_USER_RECORD:
        return None
    if not (flags & RECORD_IN_USE) or (flags & RECORD_DIRECTORY):
        return None
    if _u64(fixed, 32) & FILE_REFERENCE_MASK:
        return None
    if any(attr.atype == ATTR_ATTRIBUTE_LIST for attr in attrs):
        return None
    data_attrs = [attr for attr in attrs
                  if attr.atype == ATTR_DATA and not attr.name and attr.nonresident]
    if len(data_attrs) != 1:
        return None
    attr = data_attrs[0]
    if attr.lowest_vcn != 0 or attr.flags & (ATTR_COMPRESSED | ATTR_ENCRYPTED | ATTR_SPARSE):
        return None
    if any(run.lcn is None for run in attr.runs):
        return None
    if not attr.runs or attr.data_size == 0:
        return None
    return attr


def _stream_blocker_reason(record_number: int, fixed: bytes | bytearray,
                           attrs: list[Attribute], attr: Attribute,
                           movable_attr: Attribute | None) -> str:
    if movable_attr is not None and attr.offset == movable_attr.offset:
        return ""
    flags = _u16(fixed, 22)
    base_record = _u64(fixed, 32) & FILE_REFERENCE_MASK
    if record_number < FIRST_USER_RECORD:
        return "NTFS system metadata is not yet movable"
    if flags & RECORD_DIRECTORY:
        return "directory data and index streams are not yet movable"
    if base_record:
        return "attribute-list extension records are not yet movable"
    if any(item.atype == ATTR_ATTRIBUTE_LIST for item in attrs):
        return "streams described through $ATTRIBUTE_LIST are not yet movable"
    if attr.atype == ATTR_DATA:
        if attr.name:
            return "named NTFS data streams are not yet movable"
        if attr.flags & ATTR_COMPRESSED:
            return "compressed NTFS data is not yet movable"
        if attr.flags & ATTR_SPARSE or any(run.lcn is None for run in attr.runs):
            return "sparse NTFS data is not yet movable"
        if attr.flags & ATTR_ENCRYPTED:
            return "encrypted NTFS data is not yet movable"
        if attr.lowest_vcn != 0:
            return "split NTFS data-stream segments are not yet movable"
        unnamed = [item for item in attrs
                   if item.atype == ATTR_DATA and not item.name and item.nonresident]
        if len(unnamed) != 1:
            return "multiple unnamed NTFS data segments are not yet movable"
        return "this NTFS data-stream layout is not yet movable"
    if attr.atype == ATTR_INDEX_ALLOCATION:
        return "directory index allocation is not yet movable"
    return f"the {ATTRIBUTE_NAMES.get(attr.atype, f'attribute 0x{attr.atype:x}')} stream is not yet movable"


def _iter_mft_records(layout: NtfsLayout) -> Iterator[tuple[int, bytes]]:
    volume = layout.volume
    record_size = volume.mft_record_size
    record_count = min(layout.mft_data_size // record_size, 0xFFFFFFFF)
    records_per_chunk = max(1, MFT_RECORD_CHUNK // record_size)
    first = 0
    while first < record_count:
        count = min(records_per_chunk, record_count - first)
        raw = _read_stream(volume, layout.mft_runs, first * record_size, count * record_size)
        for offset in range(count):
            start = offset * record_size
            yield first + offset, raw[start:start + record_size]
        first += count


def _record_hibernation_active(layout: NtfsLayout, fixed: bytes | bytearray,
                               attrs: list[Attribute], file_name: str) -> bool:
    if file_name.casefold() != "hiberfil.sys":
        return False
    data_attrs = [attr for attr in attrs if attr.atype == ATTR_DATA and not attr.name]
    if not data_attrs:
        return False
    attr = data_attrs[0]
    if attr.nonresident:
        if not attr.runs or attr.data_size == 0:
            return False
        header = _read_stream(layout.volume, attr.runs, 0, min(4096, attr.data_size))
    else:
        header = _resident_value(fixed, attr)
    return bool(header[:4].strip(b"\0"))


def _scan_allocation_plan(layout: NtfsLayout) -> AllocationPlan:
    streams: dict[tuple[int, int], StreamInfo] = {}
    names: dict[int, str] = {}
    malformed = 0
    hibernation = False
    movable_count = 0
    for number, raw in _iter_mft_records(layout):
        if raw[:4] != b"FILE":
            continue
        try:
            fixed = _apply_fixups(raw, layout.volume.bytes_per_sector)
            if not (_u16(fixed, 22) & RECORD_IN_USE):
                continue
            attrs = list(_attributes(fixed))
        except NtfsCompactError:
            malformed += 1
            continue
        file_name = _best_file_name(fixed, attrs, number)
        if file_name:
            names[number] = file_name
        if not hibernation and _record_hibernation_active(layout, fixed, attrs, file_name):
            hibernation = True
        base_record = _u64(fixed, 32) & FILE_REFERENCE_MASK
        movable_attr = _select_movable_attribute(number, fixed, attrs)
        if movable_attr is not None:
            movable_count += 1
        for attr in attrs:
            if not attr.nonresident:
                continue
            physical_runs = tuple(run for run in attr.runs if run.lcn is not None)
            if not physical_runs:
                continue
            for run in physical_runs:
                if run.lcn < 0 or run.lcn + run.length > layout.volume.total_clusters:
                    raise NtfsCompactError(
                        f"MFT record {number} describes clusters outside the NTFS volume"
                    )
            info = StreamInfo(
                record_number=number,
                base_record_number=int(base_record),
                attribute_offset=attr.offset,
                attribute_type=attr.atype,
                attribute_name=_decode_attribute_name(attr.name),
                file_name=file_name,
                flags=attr.flags,
                runs=tuple(attr.runs),
                movable=movable_attr is not None and attr.offset == movable_attr.offset,
                blocker_reason=_stream_blocker_reason(number, fixed, attrs, attr, movable_attr),
            )
            streams[info.key] = info

    # Extension records often have no $FILE_NAME of their own.  Resolve their
    # base-record name after the complete MFT pass so scan order does not matter.
    for key, info in list(streams.items()):
        if not info.file_name and info.base_record_number in names:
            streams[key] = StreamInfo(
                record_number=info.record_number,
                base_record_number=info.base_record_number,
                attribute_offset=info.attribute_offset,
                attribute_type=info.attribute_type,
                attribute_name=info.attribute_name,
                file_name=names[info.base_record_number],
                flags=info.flags,
                runs=info.runs,
                movable=info.movable,
                blocker_reason=info.blocker_reason,
                generation=info.generation,
            )

    heap = [(-info.highest_lcn, info.record_number, info.attribute_offset, info.generation)
            for info in streams.values() if info.highest_lcn]
    heapq.heapify(heap)
    return AllocationPlan(streams, heap, movable_count, malformed, hibernation)


def _heap_entry_current(plan: AllocationPlan, entry: tuple[int, int, int, int]) -> bool:
    neg_high, record_number, attribute_offset, generation = entry
    info = plan.streams.get((record_number, attribute_offset))
    return bool(info is not None and info.generation == generation and info.highest_lcn == -neg_high)


def _clean_plan_heap(plan: AllocationPlan) -> None:
    while plan.heap and not _heap_entry_current(plan, plan.heap[0]):
        heapq.heappop(plan.heap)


def _owners_at_high_water(plan: AllocationPlan, high_water: int) -> tuple[int, list[StreamInfo]]:
    _clean_plan_heap(plan)
    if not plan.heap:
        return 0, []
    metadata_high = -plan.heap[0][0]
    if metadata_high != high_water:
        return metadata_high, []
    held: list[tuple[int, int, int, int]] = []
    owners: list[StreamInfo] = []
    while plan.heap and -plan.heap[0][0] == high_water:
        entry = heapq.heappop(plan.heap)
        if not _heap_entry_current(plan, entry):
            continue
        held.append(entry)
        info = plan.streams[(entry[1], entry[2])]
        if any(run.lcn is not None and run.lcn <= high_water - 1 < run.lcn + run.length
               for run in info.runs):
            owners.append(info)
    for entry in held:
        heapq.heappush(plan.heap, entry)
    return metadata_high, owners


def _load_candidate(layout: NtfsLayout, info: StreamInfo) -> Candidate:
    record_offset, raw, fixed = _read_mft_record(layout.volume, layout.mft_runs, info.record_number)
    if fixed[:4] != b"FILE" or not (_u16(fixed, 22) & RECORD_IN_USE):
        raise NtfsCompactError(f"MFT record {info.record_number} changed during offline compaction")
    attrs = list(_attributes(fixed))
    attr = _select_movable_attribute(info.record_number, fixed, attrs)
    if attr is None or attr.offset != info.attribute_offset:
        raise NtfsCompactError(
            f"MFT record {info.record_number} is no longer a supported movable stream"
        )
    return Candidate(info.record_number, record_offset, raw, bytes(fixed), attr)


def _update_moved_stream(plan: AllocationPlan, info: StreamInfo, destination: int,
                         clusters: int) -> StreamInfo:
    updated = StreamInfo(
        record_number=info.record_number,
        base_record_number=info.base_record_number,
        attribute_offset=info.attribute_offset,
        attribute_type=info.attribute_type,
        attribute_name=info.attribute_name,
        file_name=info.file_name,
        flags=info.flags,
        runs=(Run(destination, clusters),),
        movable=True,
        blocker_reason="",
        generation=info.generation + 1,
    )
    plan.streams[updated.key] = updated
    heapq.heappush(plan.heap, (-updated.highest_lcn, updated.record_number,
                               updated.attribute_offset, updated.generation))
    return updated


def _attribute_display(info: StreamInfo) -> str:
    label = ATTRIBUTE_NAMES.get(info.attribute_type, f"attribute 0x{info.attribute_type:x}")
    if info.attribute_name:
        label += f' named "{info.attribute_name}"'
    return label


def _stream_display(info: StreamInfo) -> str:
    if info.record_number in SYSTEM_RECORD_NAMES:
        owner = SYSTEM_RECORD_NAMES[info.record_number]
    else:
        owner = f"MFT record {info.record_number}"
        if info.file_name:
            owner += f' ("{info.file_name}")'
    if info.base_record_number:
        owner += f" via extension record for MFT {info.base_record_number}"
    return f"{owner} {_attribute_display(info)}"


def _allocated_cluster_count(bitmap: bytes | bytearray, total_clusters: int) -> int:
    full_bytes, remaining = divmod(total_clusters, 8)
    total = sum(int(value).bit_count() for value in bitmap[:full_bytes])
    if remaining and full_bytes < len(bitmap):
        total += (bitmap[full_bytes] & ((1 << remaining) - 1)).bit_count()
    return total


def _boundary_progress(initial_high: int, current_high: int, theoretical_floor: int) -> float:
    if initial_high <= theoretical_floor:
        return 100.0
    reduced = max(0, initial_high - current_high)
    possible = max(1, initial_high - theoretical_floor)
    return max(0.0, min(100.0, 100.0 * reduced / possible))


def _human_bytes(value: int) -> str:
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or suffix == "TiB":
            return f"{amount:.1f} {suffix}" if suffix != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{value} B"


def _candidate_records(layout: NtfsLayout) -> list[Candidate]:
    """Compatibility helper used by focused tests and diagnostics."""
    plan = _scan_allocation_plan(layout)
    candidates = [_load_candidate(layout, info)
                  for info in plan.streams.values() if info.movable]
    candidates.sort(key=lambda item: item.highest_lcn, reverse=True)
    return candidates


def _updated_record(candidate: Candidate, destination: int, sector_size: int) -> bytes:
    fixed = bytearray(candidate.record_fixed)
    attr = candidate.attribute
    new_runs = (Run(destination, candidate.clusters),)
    encoded = _encode_runlist(new_runs)
    capacity = attr.length - attr.run_offset
    if len(encoded) > capacity:
        raise NtfsCompactError("replacement NTFS mapping pairs do not fit the existing attribute")
    start = attr.offset + attr.run_offset
    fixed[start:start + capacity] = encoded + b"\0" * (capacity - len(encoded))
    return _prepare_fixups(fixed, sector_size)


def _copy_runs(volume: Volume, runs: Iterable[Run], destination_lcn: int, clusters: int) -> None:
    destination = destination_lcn * volume.cluster_size
    remaining = clusters * volume.cluster_size
    written = 0
    for run in runs:
        if run.lcn is None:
            raise NtfsCompactError("sparse source stream cannot be compacted")
        source = run.lcn * volume.cluster_size
        run_bytes = run.length * volume.cluster_size
        consumed = 0
        while consumed < run_bytes:
            if _stop_requested:
                # A stop before metadata reservation is harmless.  Finish the
                # current copy chunk so no partial write call is interrupted.
                pass
            take = min(COPY_CHUNK, run_bytes - consumed)
            data = _pread_exact(volume.fd, take, source + consumed)
            _pwrite_exact(volume.fd, data, destination + written)
            consumed += take
            written += take
            remaining -= take
    if remaining != 0:
        raise NtfsCompactError("source runlist length changed during copy")
    os.fsync(volume.fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_journal(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_journal(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NtfsCompactError(f"cannot read NTFS recovery journal: {exc}") from exc
    if state.get("schema") != SCHEMA or state.get("kind") != JOURNAL_KIND:
        raise NtfsCompactError("recovery journal is not a native NTFS move journal")
    return state


def _remove_journal(path: Path) -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        pass


def _journal_state(layout: NtfsLayout, candidate: Candidate, destination: int,
                   new_record: bytes, bitmap_before: list[tuple[int, bytes]],
                   volume_records: tuple[bytes, bytes, bytes, bytes], stage: str) -> dict:
    return {
        "schema": SCHEMA,
        "kind": JOURNAL_KIND,
        "stage": stage,
        "device": layout.volume.path,
        "device_size": layout.volume.device_size,
        "serial": layout.volume.serial,
        "record_number": candidate.record_number,
        "record_raw_before": base64.b64encode(candidate.record_raw).decode("ascii"),
        "record_raw_after": base64.b64encode(new_record).decode("ascii"),
        "source_runs": [[run.lcn, run.length] for run in candidate.attribute.runs],
        "destination": destination,
        "clusters": candidate.clusters,
        "bitmap_before": [[offset, base64.b64encode(data).decode("ascii")]
                          for offset, data in bitmap_before],
        "volume_record_before": base64.b64encode(volume_records[0]).decode("ascii"),
        "volume_record_dirty": base64.b64encode(volume_records[1]).decode("ascii"),
        "mirror_record_before": base64.b64encode(volume_records[2]).decode("ascii"),
        "mirror_record_dirty": base64.b64encode(volume_records[3]).decode("ascii"),
    }

def _validate_identity(layout: NtfsLayout, state: dict) -> None:
    volume = layout.volume
    if os.path.realpath(str(state.get("device", ""))) != volume.path:
        raise NtfsCompactError("recovery journal names a different NTFS target")
    if int(state.get("device_size", -1)) != volume.device_size:
        raise NtfsCompactError("NTFS target size changed after the interrupted operation")
    if str(state.get("serial", "")) != volume.serial:
        raise NtfsCompactError("NTFS serial does not match the recovery journal")


def _affected_clusters(state: dict) -> tuple[list[int], list[int]]:
    old: list[int] = []
    for lcn, length in state["source_runs"]:
        if lcn is None:
            raise NtfsCompactError("journal contains an unsupported sparse source run")
        old.extend(range(int(lcn), int(lcn) + int(length)))
    dest = int(state["destination"])
    count = int(state["clusters"])
    return old, list(range(dest, dest + count))


def _recover_loaded(layout: NtfsLayout, state: dict, journal_path: Path) -> int:
    _validate_identity(layout, state)
    volume = layout.volume
    record_number = int(state["record_number"])
    before = base64.b64decode(state["record_raw_before"], validate=True)
    after = base64.b64decode(state["record_raw_after"], validate=True)
    if len(before) != volume.mft_record_size or len(after) != volume.mft_record_size:
        raise NtfsCompactError("recovery journal contains an invalid MFT record image")
    current = _read_stream(volume, layout.mft_runs, record_number * volume.mft_record_size,
                           volume.mft_record_size)
    old_clusters, new_clusters = _affected_clusters(state)
    snapshots = [(int(offset), base64.b64decode(encoded, validate=True))
                 for offset, encoded in state.get("bitmap_before", [])]
    if not snapshots:
        raise NtfsCompactError("recovery journal does not contain bitmap snapshots")

    if current == after:
        # Metadata points at the copied destination. Complete forward by
        # reserving the destination and releasing every old cluster.
        for cluster in new_clusters:
            _set_bit(layout.bitmap, cluster, True)
        for cluster in old_clusters:
            _set_bit(layout.bitmap, cluster, False)
        _write_bitmap_patches(layout, _current_bitmap_patches(layout, snapshots))
        os.fsync(volume.fd)
        print("Recovered native NTFS transaction forward.", flush=True)
    else:
        # Metadata is old or torn. Restore the original record and the exact
        # bitmap bytes saved before the move. Source clusters are never
        # overwritten, so rollback always retains the authoritative data.
        _write_mft_record(volume, layout.mft_runs, record_number, before)
        _write_bitmap_patches(layout, snapshots)
        os.fsync(volume.fd)
        print("Rolled native NTFS transaction back to its original mapping.", flush=True)
    volume_before = base64.b64decode(state["volume_record_before"], validate=True)
    mirror_before = base64.b64decode(state["mirror_record_before"], validate=True)
    if len(volume_before) != volume.mft_record_size or len(mirror_before) != volume.mft_record_size:
        raise NtfsCompactError("recovery journal contains invalid $Volume record images")
    _write_volume_records(layout, volume_before, mirror_before)
    os.fsync(volume.fd)
    _remove_journal(journal_path)
    return 0

def recover(device: str, journal_path: Path) -> int:
    if _is_mounted(device):
        raise NtfsCompactError("NTFS recovery requires an unmounted volume")
    state = _read_journal(journal_path)
    volume = _open_volume(device, True)
    try:
        layout = _read_layout(volume, allow_dirty=True, check_volume=False)
        return _recover_loaded(layout, state, journal_path)
    finally:
        _close_volume(volume)


def _detail(diagnostic: TextIO | None, message: str) -> None:
    if diagnostic is not None:
        diagnostic.write(message + "\n")
        diagnostic.flush()


def _move_one(layout: NtfsLayout, candidate: Candidate, destination: int,
              journal_path: Path, diagnostic: TextIO | None = None) -> None:
    volume = layout.volume
    new_record = _updated_record(candidate, destination, volume.bytes_per_sector)
    old_clusters = [cluster for run in candidate.attribute.runs
                    for cluster in range(int(run.lcn), int(run.lcn) + run.length)]
    new_clusters = list(range(destination, destination + candidate.clusters))
    bitmap_before = _bitmap_patches(layout, old_clusters + new_clusters)
    volume_records = _volume_record_state(layout)

    _detail(
        diagnostic,
        f"MFT record {candidate.record_number}: {candidate.clusters} clusters "
        f"from high LCN {candidate.highest_lcn - 1} to LCN {destination}",
    )
    _copy_runs(volume, candidate.attribute.runs, destination, candidate.clusters)
    state = _journal_state(layout, candidate, destination, new_record,
                           bitmap_before, volume_records, "copied")
    _write_journal(journal_path, state)

    _write_volume_records(layout, volume_records[1], volume_records[3])
    os.fsync(volume.fd)
    state["stage"] = "volume-dirty"
    _write_journal(journal_path, state)

    for cluster in new_clusters:
        _set_bit(layout.bitmap, cluster, True)
    _write_bitmap_patches(layout, _current_bitmap_patches(layout, bitmap_before))
    os.fsync(volume.fd)
    state["stage"] = "destination-allocated"
    _write_journal(journal_path, state)

    _write_mft_record(volume, layout.mft_runs, candidate.record_number, new_record)
    os.fsync(volume.fd)
    state["stage"] = "metadata-switched"
    _write_journal(journal_path, state)

    for cluster in old_clusters:
        _set_bit(layout.bitmap, cluster, False)
    _write_bitmap_patches(layout, _current_bitmap_patches(layout, bitmap_before))
    os.fsync(volume.fd)
    state["stage"] = "old-released"
    _write_journal(journal_path, state)

    _write_volume_records(layout, volume_records[0], volume_records[2])
    os.fsync(volume.fd)
    state["stage"] = "volume-clean"
    _write_journal(journal_path, state)
    _remove_journal(journal_path)


def _blocker_message(high_water: int, metadata_high: int,
                     owners: list[StreamInfo]) -> str:
    cluster = high_water - 1
    if metadata_high > high_water:
        return (
            f"NTFS metadata describes allocation through cluster {metadata_high - 1:,}, "
            f"but $Bitmap ends at cluster {cluster:,}; compaction stopped because the "
            "metadata and allocation bitmap disagree"
        )
    if not owners:
        return (
            f"allocated high-water cluster {cluster:,} is not claimed by any readable "
            "non-resident MFT stream; it may be reserved metadata, an orphan allocation, "
            "or damaged metadata"
        )
    if len(owners) > 1:
        labels = "; ".join(_stream_display(owner) for owner in owners[:4])
        suffix = "" if len(owners) <= 4 else f"; and {len(owners) - 4} more"
        return (
            f"multiple NTFS streams claim high-water cluster {cluster:,}: "
            f"{labels}{suffix}; compaction stopped to avoid modifying a cross-linked volume"
        )
    owner = owners[0]
    reason = owner.blocker_reason or "the stream cannot be relocated by the current native writer"
    return f"{_stream_display(owner)} at cluster {cluster:,}; {reason}"


def compact(device: str, journal_path: Path,
            diagnostic_path: Path | None = None) -> int:
    if _is_mounted(device):
        raise NtfsCompactError("NTFS compaction requires an unmounted volume")
    if journal_path.exists():
        raise NtfsCompactError("an unfinished NTFS transaction exists; run Recover first")
    diagnostic: TextIO | None = None
    volume = _open_volume(device, True)
    try:
        if diagnostic_path is not None:
            diagnostic_path = diagnostic_path.expanduser().resolve()
            if diagnostic_path == journal_path.expanduser().resolve():
                raise NtfsCompactError("the diagnostic log and recovery journal must be different files")
            diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostic = diagnostic_path.open("w", encoding="utf-8", buffering=1)
            os.chmod(diagnostic_path, 0o600)
            print(f"Detailed NTFS move diagnostics: {diagnostic_path}", flush=True)

        layout = _read_layout(volume)
        plan = _scan_allocation_plan(layout)
        if plan.hibernation_active:
            raise NtfsCompactError(
                "Windows hibernation is active on this NTFS volume; perform a full Windows shutdown first"
            )
        before_high = _highest_used(layout.bitmap, volume.total_clusters)
        allocated = _allocated_cluster_count(layout.bitmap, volume.total_clusters)
        theoretical_floor = allocated
        print(
            f"Native NTFS scan found {plan.movable_count:,} movable ordinary file streams "
            f"and tracked {len(plan.streams):,} physical NTFS streams.",
            flush=True,
        )
        if plan.malformed_records:
            print(
                f"Warning: {plan.malformed_records:,} in-use MFT records could not be decoded; "
                "their allocations will be treated as immovable.",
                flush=True,
            )
        print(f"Initial allocated high-water mark: cluster {before_high - 1:,}.", flush=True)
        print(
            f"Theoretical packed boundary from {allocated:,} allocated clusters: "
            f"cluster {max(0, theoretical_floor - 1):,}.",
            flush=True,
        )
        print("0.00 percent completed", flush=True)

        moved_files = 0
        moved_clusters = 0
        blocker = ""
        last_report_files = 0
        last_report_clusters = 0
        last_report_time = time.monotonic()

        while True:
            if _stop_requested:
                break
            current_high = _highest_used(layout.bitmap, volume.total_clusters)
            if current_high <= theoretical_floor:
                break
            metadata_high, owners = _owners_at_high_water(plan, current_high)
            if metadata_high != current_high or len(owners) != 1:
                blocker = _blocker_message(current_high, metadata_high, owners)
                break
            owner = owners[0]
            if not owner.movable:
                blocker = _blocker_message(current_high, metadata_high, owners)
                break

            candidate = _load_candidate(layout, owner)
            if candidate.highest_lcn != current_high:
                blocker = (
                    f"{_stream_display(owner)} changed while the offline compact plan was active; "
                    "no further streams were moved"
                )
                break
            destination = _find_free_run(layout.bitmap, candidate.clusters, candidate.lowest_lcn)
            if destination is None or destination + candidate.clusters >= candidate.highest_lcn:
                blocker = (
                    f"{_stream_display(owner)} at cluster {current_high - 1:,}; "
                    f"no lower contiguous free run of {candidate.clusters:,} clusters is available"
                )
                break
            try:
                _updated_record(candidate, destination, volume.bytes_per_sector)
            except NtfsCompactError:
                blocker = (
                    f"{_stream_display(owner)} at cluster {current_high - 1:,}; "
                    "replacement mapping pairs do not fit its existing MFT attribute"
                )
                break

            _move_one(layout, candidate, destination, journal_path, diagnostic)
            moved_files += 1
            moved_clusters += candidate.clusters
            _update_moved_stream(plan, owner, destination, candidate.clusters)
            new_high = _highest_used(layout.bitmap, volume.total_clusters)
            if new_high >= current_high:
                blocker = (
                    f"moving {_stream_display(owner)} did not lower the allocated boundary; "
                    "compaction stopped rather than relocating unrelated lower files"
                )
                break
            progress = _boundary_progress(before_high, new_high, theoretical_floor)
            print(f"{progress:.2f} percent completed", flush=True)

            now = time.monotonic()
            if (moved_files - last_report_files >= REPORT_EVERY_FILES or
                    moved_clusters - last_report_clusters >= REPORT_EVERY_CLUSTERS or
                    now - last_report_time >= REPORT_EVERY_SECONDS):
                print(
                    f"Moved {moved_files:,} high-water files ({_human_bytes(moved_clusters * volume.cluster_size)}); "
                    f"current high-water cluster {new_high - 1:,}.",
                    flush=True,
                )
                last_report_files = moved_files
                last_report_clusters = moved_clusters
                last_report_time = now

        after_high = _highest_used(layout.bitmap, volume.total_clusters)
        reduced = max(0, before_high - after_high)
        print(
            f"Native NTFS compact moved {moved_files:,} files and {moved_clusters:,} clusters "
            f"({_human_bytes(moved_clusters * volume.cluster_size)}).",
            flush=True,
        )
        print(
            f"Allocated high-water mark: cluster {before_high - 1:,} -> {after_high - 1:,}.",
            flush=True,
        )
        if reduced:
            print(
                f"Effective NTFS boundary reduction: {reduced:,} clusters "
                f"({_human_bytes(reduced * volume.cluster_size)}).",
                flush=True,
            )
        else:
            print(
                "No effective NTFS volume compaction was achieved; the allocated high-water "
                "boundary did not move.",
                flush=True,
            )
        if blocker:
            print(f"Compaction limited by {blocker}.", flush=True)
        elif after_high <= theoretical_floor:
            print("The allocation boundary reached the theoretical packed limit.", flush=True)
        print(
            "Only the stream that currently owns the physical high-water cluster is considered; "
            "unrelated lower files are not moved.",
            flush=True,
        )
        if _stop_requested:
            print("Stopped safely after the current NTFS stream transaction.", flush=True)
        return 0
    finally:
        if diagnostic is not None:
            diagnostic.close()
        _close_volume(volume)


def _validate_confirmation(device: str, confirmed: str | None, write: bool) -> None:
    if not write:
        raise NtfsCompactError("write mode requires --write")
    if not confirmed or os.path.realpath(confirmed) != os.path.realpath(device):
        raise NtfsCompactError("--confirm must name the exact target device")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native offline NTFS compaction and recovery")
    parser.add_argument("operation", choices=("compact", "recover"))
    parser.add_argument("device")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--ram-buffer", default="auto")
    parser.add_argument("--workers", default="auto")
    parser.add_argument("--live-map-cells")
    parser.add_argument("--diagnostic-log", help="optional detailed per-stream move log")
    args = parser.parse_args(argv)
    try:
        _validate_confirmation(args.device, args.confirm, args.write)
        journal = Path(args.journal)
        if args.operation == "compact":
            diagnostic = Path(args.diagnostic_log) if args.diagnostic_log else None
            return compact(args.device, journal, diagnostic)
        return recover(args.device, journal)
    except (NtfsCompactError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    raise SystemExit(main())
