# Linux Defragger
# Author: Shannon Smith
# Purpose: Read-only NTFS allocation and fragmentation analysis plus mutation capabilities.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""NTFS allocation analysis with native offline compact and defragment."""

from __future__ import annotations
from .base import (
    BackendError, BackendInfo, CAP_ANALYSE, CAP_COMPACT, CAP_DEFRAG, CAP_MAP, CAP_RECOVER,
    FilesystemBackend, Reader, aggregate_bitmap, merge_ranges, operation, overlay_ranges,
    u16le, u32le, u64le,
)

INFO = BackendInfo(
    "ntfs", "NTFS", ("ntfs", "ntfs3"),
    CAP_ANALYSE | CAP_MAP | CAP_COMPACT | CAP_DEFRAG | CAP_RECOVER,
    "exact",
    (
        operation(
            "compact",
            "ntfs",
            warning=(
                "NTFS Compact fills lower gaps using complete higher supported file and directory "
                "streams. Every moved stream remains contiguous, so Compact does not create "
                "fragmentation merely to consume a small hole."
            ),
        ),
        operation(
            "defrag",
            "ntfs",
            warning=(
                "NTFS Defragment rebuilds each supported fragmented stream as one contiguous "
                "extent in the lowest suitable free run. Higher free space is only temporary staging."
            ),
            unsupported_options=("--transaction-files",),
        ),
        operation("recover", "ntfs"),
    ),
)

_ATTR_DATA = 0x80
_ATTR_INDEX_ALLOCATION = 0xA0
_RECORD_IN_USE = 0x0001
_RECORD_DIRECTORY = 0x0002
_FILE_REFERENCE_MASK = (1 << 48) - 1
_MFT_READ_CHUNK = 16 * 1024 * 1024
_FIRST_USER_RECORD = 24


def _signed_le(data: bytes) -> int:
    if not data:
        return 0
    return int.from_bytes(data, "little", signed=True)


def _runlist(data: bytes):
    pos = 0
    lcn = 0
    while pos < len(data):
        header = data[pos]
        pos += 1
        if header == 0:
            return
        lsz = header & 0x0F
        osz = header >> 4
        if not lsz or pos + lsz + osz > len(data):
            raise BackendError("invalid NTFS runlist")
        length = int.from_bytes(data[pos:pos+lsz], "little")
        pos += lsz
        if length <= 0:
            raise BackendError("invalid zero-length NTFS run")
        delta = _signed_le(data[pos:pos+osz]) if osz else None
        pos += osz
        if delta is None:
            yield None, length
        else:
            lcn += delta
            if lcn < 0:
                raise BackendError("invalid negative NTFS logical cluster number")
            yield lcn, length


def _fixup(record: bytearray, sector_size: int) -> None:
    usa_off = u16le(record, 4)
    usa_count = u16le(record, 6)
    if usa_count < 1 or usa_off + usa_count * 2 > len(record):
        raise BackendError("invalid NTFS update sequence array")
    usn = record[usa_off:usa_off+2]
    for i in range(1, usa_count):
        end = i * sector_size
        if end > len(record) or record[end-2:end] != usn:
            raise BackendError("NTFS MFT fixup mismatch")
        record[end-2:end] = record[usa_off+i*2:usa_off+i*2+2]


def _attributes(record: bytes):
    """Yield validated attribute records from one fixed-up MFT record."""
    pos = u16le(record, 20)
    bytes_in_use = min(len(record), u32le(record, 24))
    while pos + 16 <= bytes_in_use:
        atype = u32le(record, pos)
        if atype == 0xFFFFFFFF:
            return
        alen = u32le(record, pos + 4)
        if alen < 24 or pos + alen > bytes_in_use:
            raise BackendError("invalid NTFS attribute")
        nonresident = bool(record[pos + 8])
        name_len = record[pos + 9]
        name_off = u16le(record, pos + 10)
        if name_len:
            name_end = name_off + name_len * 2
            if name_off < 16 or name_end > alen:
                raise BackendError("invalid NTFS attribute name")
            name = bytes(record[pos + name_off:pos + name_end])
        else:
            name = b""
        if nonresident:
            if alen < 64:
                raise BackendError("truncated NTFS non-resident attribute")
            lowest_vcn = u64le(record, pos + 16)
            highest_vcn = u64le(record, pos + 24)
            run_off = u16le(record, pos + 32)
            if run_off < 64 or run_off >= alen:
                raise BackendError("invalid NTFS runlist offset")
            runs = list(_runlist(bytes(record[pos + run_off:pos + alen])))
            run_clusters = sum(length for _lcn, length in runs)
            expected = highest_vcn - lowest_vcn + 1
            if run_clusters != expected:
                raise BackendError("NTFS runlist length does not match its VCN range")
            yield {
                "type": atype,
                "name": name,
                "nonresident": True,
                "lowest_vcn": lowest_vcn,
                "highest_vcn": highest_vcn,
                "runs": runs,
                "data_size": u64le(record, pos + 48),
            }
        else:
            yield {"type": atype, "name": name, "nonresident": False}
        pos += alen


def _mft_stream(record: bytes):
    """Return the complete unnamed $MFT data stream described by record zero."""
    segments = []
    data_size = None
    for attr in _attributes(record):
        if attr["type"] != _ATTR_DATA or attr["name"] or not attr["nonresident"]:
            continue
        segments.append((int(attr["lowest_vcn"]), list(attr["runs"])))
        if int(attr["lowest_vcn"]) == 0:
            data_size = int(attr["data_size"])
    if data_size is None or not segments:
        raise BackendError("NTFS $MFT data attribute was not found")
    segments.sort(key=lambda item: item[0])
    next_vcn = 0
    for lowest_vcn, runs in segments:
        if lowest_vcn != next_vcn:
            raise BackendError("NTFS $MFT runlist is split through an unsupported attribute-list extension")
        next_vcn += sum(length for _lcn, length in runs)
    return segments, data_size


def _mft_records(reader: Reader, segments, data_size: int, cluster_size: int, record_size: int):
    """Stream fixed-size MFT records without loading the full MFT into memory."""
    if data_size < record_size:
        raise BackendError("NTFS $MFT data stream is shorter than one record")
    remaining = data_size
    expected_vcn = 0
    buffer = bytearray()
    record_number = 0
    for lowest_vcn, runs in segments:
        if lowest_vcn != expected_vcn:
            raise BackendError("non-contiguous NTFS $MFT VCN coverage")
        for lcn, length in runs:
            run_bytes = min(remaining, length * cluster_size)
            if lcn is None:
                raise BackendError("NTFS $MFT contains a sparse run")
            physical = lcn * cluster_size
            consumed = 0
            while consumed < run_bytes:
                take = min(_MFT_READ_CHUNK, run_bytes - consumed)
                buffer.extend(reader.read(physical + consumed, take))
                consumed += take
                offset = 0
                while len(buffer) - offset >= record_size:
                    yield record_number, bytes(buffer[offset:offset + record_size])
                    offset += record_size
                    record_number += 1
                if offset:
                    del buffer[:offset]
            remaining -= run_bytes
            expected_vcn += length
            if remaining <= 0:
                return
    if remaining > 0:
        raise BackendError("NTFS $MFT runlist is shorter than its data size")


def _stream_fragments(segments) -> int:
    """Count physical extents after joining adjacent runlist segments."""
    fragments = 0
    previous_lcn_end = None
    previous_vcn_end = None
    broken = True
    for lowest_vcn, runs in sorted(segments, key=lambda item: item[0]):
        if previous_vcn_end is None or lowest_vcn != previous_vcn_end:
            broken = True
        vcn = lowest_vcn
        for lcn, length in runs:
            if lcn is None:
                broken = True
                previous_lcn_end = None
            else:
                if broken or previous_lcn_end != lcn:
                    fragments += 1
                previous_lcn_end = lcn + length
                broken = False
            vcn += length
        previous_vcn_end = vcn
    return fragments




def _stream_ranges(streams: dict) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for segments in streams.values():
        for _lowest_vcn, runs in segments:
            for lcn, length in runs:
                if lcn is not None:
                    ranges.append((lcn, lcn + length))
    return ranges




def _scan_fragmentation(reader: Reader, mft_lcn: int, cluster_size: int,
                        record_size: int, sector_size: int) -> dict:
    """Count in-use NTFS objects whose data or index stream has multiple extents."""
    record_zero = bytearray(reader.read(mft_lcn * cluster_size, record_size))
    if record_zero[:4] != b"FILE":
        raise BackendError("NTFS $MFT record zero was not found")
    _fixup(record_zero, sector_size)
    segments, data_size = _mft_stream(bytes(record_zero))

    objects: dict[int, dict] = {}
    metadata_ranges: list[tuple[int, int]] = []
    records_scanned = 0
    malformed_records = 0
    for record_number, raw in _mft_records(reader, segments, data_size, cluster_size, record_size):
        records_scanned += 1
        if raw[:4] != b"FILE":
            continue
        record = bytearray(raw)
        try:
            _fixup(record, sector_size)
        except BackendError:
            malformed_records += 1
            continue
        flags = u16le(record, 22)
        if not (flags & _RECORD_IN_USE):
            continue
        base_reference = u64le(record, 32) & _FILE_REFERENCE_MASK
        owner = base_reference or record_number
        state = objects.setdefault(owner, {"base": False, "directory": False, "streams": {}})
        if not base_reference:
            state["base"] = True
            state["directory"] = bool(flags & _RECORD_DIRECTORY)
        try:
            attrs = _attributes(bytes(record))
            for attr in attrs:
                if not attr["nonresident"]:
                    continue
                atype = int(attr["type"])
                runs = list(attr["runs"])
                physical = [(int(lcn), int(lcn) + int(length))
                            for lcn, length in runs if lcn is not None]
                is_directory = bool(state["directory"])
                is_object_stream = (
                    (not is_directory and atype == _ATTR_DATA) or
                    (is_directory and atype == _ATTR_INDEX_ALLOCATION)
                )
                # Preserve the analyser's historical object/fragment counts for
                # every in-use MFT record, including the synthetic core records
                # used by regression images. Independently classify the first
                # NTFS system records and all non-file streams as filesystem
                # metadata so the allocation map does not present them as
                # ordinary files left behind by Compact.
                if record_number < _FIRST_USER_RECORD or not is_object_stream:
                    metadata_ranges.extend(physical)
                if not is_object_stream:
                    continue
                key = (atype, bytes(attr["name"]))
                stream = state["streams"].setdefault(key, [])
                stream.append((int(attr["lowest_vcn"]), runs))
        except BackendError:
            malformed_records += 1

    regular_files = directories = fragmented_files = fragmented_directories = 0
    fragmented_ranges: list[tuple[int, int]] = []
    directory_ranges: list[tuple[int, int]] = []
    for state in objects.values():
        if not state["base"]:
            continue
        fragmented = any(_stream_fragments(parts) > 1 for parts in state["streams"].values())
        physical_ranges = _stream_ranges(state["streams"])
        if state["directory"]:
            directories += 1
            directory_ranges.extend(physical_ranges)
            if fragmented:
                fragmented_directories += 1
                fragmented_ranges.extend(physical_ranges)
        else:
            regular_files += 1
            if fragmented:
                fragmented_files += 1
                fragmented_ranges.extend(physical_ranges)

    return {
        "regular_files": regular_files,
        "directories": directories,
        "fragmented_files": fragmented_files,
        "fragmented_directories": fragmented_directories,
        "fragmentation_percent": 100.0 * fragmented_files / max(1, regular_files),
        "mft_records_scanned": records_scanned,
        "mft_malformed_records": malformed_records,
        "fragmented_ranges": merge_ranges(fragmented_ranges),
        "directory_ranges": merge_ranges(directory_ranges),
        "metadata_ranges": merge_ranges(metadata_ranges),
    }


class NtfsBackend(FilesystemBackend):
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            return r.read(3, 8) == b"NTFS    "

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            bs = r.read(0, 512)
            if bs[3:11] != b"NTFS    ":
                raise BackendError("not an NTFS volume")
            bps = u16le(bs, 11)
            spc_raw = bs[13]
            if bps < 256 or bps > 4096 or bps & (bps - 1):
                raise BackendError("invalid NTFS bytes-per-sector value")
            if spc_raw == 0 or spc_raw & (spc_raw - 1):
                raise BackendError("invalid NTFS sectors-per-cluster value")
            cluster_size = bps * spc_raw
            if cluster_size > 64 * 1024:
                raise BackendError("unsupported NTFS cluster size")
            total_sectors = u64le(bs, 40)
            total_clusters = total_sectors // spc_raw
            mft_lcn = u64le(bs, 48)
            rec_raw = int.from_bytes(bs[64:65], "little", signed=True)
            record_size = (1 << -rec_raw) if rec_raw < 0 else rec_raw * cluster_size
            if record_size < 512 or record_size > 1024 * 1024:
                raise BackendError("invalid NTFS MFT record size")
            # The first MFT records are guaranteed to be addressable from the MFT start in normal volumes.
            raw = bytearray(r.read(mft_lcn * cluster_size + 6 * record_size, record_size))
            if raw[:4] != b"FILE":
                raise BackendError("NTFS $Bitmap MFT record not found at record 6")
            _fixup(raw, bps)
            attr_off = u16le(raw, 20)
            bitmap_runs = None
            data_size = None
            pos = attr_off
            while pos + 16 <= len(raw):
                atype = u32le(raw, pos)
                if atype == 0xFFFFFFFF:
                    break
                alen = u32le(raw, pos + 4)
                if alen < 24 or pos + alen > len(raw):
                    raise BackendError("invalid NTFS attribute")
                nonresident = raw[pos + 8]
                name_len = raw[pos + 9]
                if atype == _ATTR_DATA and name_len == 0:
                    if nonresident:
                        run_off = u16le(raw, pos + 32)
                        data_size = u64le(raw, pos + 48)
                        bitmap_runs = list(_runlist(bytes(raw[pos+run_off:pos+alen])))
                    else:
                        value_len = u32le(raw, pos + 16)
                        value_off = u16le(raw, pos + 20)
                        bitmap = bytes(raw[pos+value_off:pos+value_off+value_len])
                        result = aggregate_bitmap(bitmap, total_clusters, cells, cluster_size, "ntfs",
                                                  details={"cluster_size": cluster_size})
                        self._add_fragmentation_summary(result, r, mft_lcn, cluster_size, record_size, bps)
                        return result
                    break
                pos += alen
            if bitmap_runs is None or data_size is None:
                raise BackendError("NTFS $Bitmap data attribute not found")
            bitmap = bytearray()
            for lcn, length in bitmap_runs:
                byte_len = length * cluster_size
                if lcn is None:
                    bitmap += b"\x00" * byte_len
                else:
                    bitmap += r.read(lcn * cluster_size, byte_len)
                if len(bitmap) >= data_size:
                    break
            bitmap = bitmap[:data_size]
            if len(bitmap) * 8 < total_clusters:
                raise BackendError("NTFS $Bitmap is shorter than the volume")
            result = aggregate_bitmap(bytes(bitmap), total_clusters, cells, cluster_size, "ntfs",
                                      details={"cluster_size": cluster_size})
            self._add_fragmentation_summary(result, r, mft_lcn, cluster_size, record_size, bps)
            return result

    @staticmethod
    def _add_fragmentation_summary(result: dict, reader: Reader, mft_lcn: int,
                                   cluster_size: int, record_size: int, sector_size: int) -> None:
        details = result.setdefault("details", {})
        try:
            summary = _scan_fragmentation(reader, mft_lcn, cluster_size, record_size, sector_size)
        except BackendError as exc:
            # Allocation mapping remains useful when an unusual attribute-list
            # layout prevents a complete file-level scan.  Report the reason
            # explicitly instead of replacing the Fragmentation card with an
            # unrelated read-only capability message.
            details["fragmentation_available"] = False
            details["fragmentation_note"] = str(exc)
            return
        fragmented_units = overlay_ranges(result["cells"], summary["fragmented_ranges"], "fragmented")
        directory_units = overlay_ranges(result["cells"], summary["directory_ranges"], "directory")
        metadata_units = overlay_ranges(result["cells"], summary["metadata_ranges"], "bad")
        result.update({
            "regular_files": summary["regular_files"],
            "directories": summary["directories"],
            "fragmented_files": summary["fragmented_files"],
            "fragmented_directories": summary["fragmented_directories"],
            "fragmentation_percent": summary["fragmentation_percent"],
        })
        details.update({
            "fragmentation_available": True,
            "fragmentation_basis": "NTFS MFT data and directory-index runlists",
            "mft_records_scanned": summary["mft_records_scanned"],
            "mft_malformed_records": summary["mft_malformed_records"],
            "fragmented_clusters_mapped": fragmented_units,
            "directory_clusters_mapped": directory_units,
            "metadata_clusters_mapped": metadata_units,
            "metadata_basis": "NTFS system and non-file nonresident streams",
        })


BACKEND = NtfsBackend()
