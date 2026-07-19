#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify NTFS read-only fragmentation analysis from MFT runlists.

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))

from backends.ntfs import BACKEND

BPS = 512
SPC = 1
CLUSTER_SIZE = BPS * SPC
RECORD_SIZE = 1024
TOTAL_CLUSTERS = 4096
MFT_LCN = 4
MFT_RECORDS = 16
MFT_CLUSTERS = MFT_RECORDS * RECORD_SIZE // CLUSTER_SIZE


def _runlist(runs: list[tuple[int | None, int]]) -> bytes:
    out = bytearray()
    previous_lcn = 0
    for lcn, length in runs:
        assert 0 < length < 256
        if lcn is None:
            out += bytes((0x01, length))
            continue
        delta = lcn - previous_lcn
        out += bytes((0x41, length))
        out += int(delta).to_bytes(4, "little", signed=True)
        previous_lcn = lcn
    out.append(0)
    return bytes(out)


def _nonresident_attr(atype: int, lowest_vcn: int, runs: list[tuple[int | None, int]],
                      data_size: int, attr_id: int = 0) -> bytes:
    mapping = _runlist(runs)
    length = (64 + len(mapping) + 7) & ~7
    attr = bytearray(length)
    struct.pack_into("<I", attr, 0, atype)
    struct.pack_into("<I", attr, 4, length)
    attr[8] = 1
    struct.pack_into("<H", attr, 14, attr_id)
    clusters = sum(run_length for _lcn, run_length in runs)
    struct.pack_into("<Q", attr, 16, lowest_vcn)
    struct.pack_into("<Q", attr, 24, lowest_vcn + clusters - 1)
    struct.pack_into("<H", attr, 32, 64)
    allocated = sum(run_length for lcn, run_length in runs if lcn is not None) * CLUSTER_SIZE
    struct.pack_into("<Q", attr, 40, allocated)
    struct.pack_into("<Q", attr, 48, data_size)
    struct.pack_into("<Q", attr, 56, data_size)
    attr[64:64 + len(mapping)] = mapping
    return bytes(attr)


def _record(number: int, *, directory: bool = False, base: int = 0,
            attrs: list[bytes] | None = None) -> bytes:
    record = bytearray(RECORD_SIZE)
    record[:4] = b"FILE"
    struct.pack_into("<H", record, 4, 0x30)
    struct.pack_into("<H", record, 6, 3)
    struct.pack_into("<H", record, 16, 1)
    struct.pack_into("<H", record, 18, 1)
    struct.pack_into("<H", record, 20, 0x38)
    flags = 0x0001 | (0x0002 if directory else 0)
    struct.pack_into("<H", record, 22, flags)
    struct.pack_into("<I", record, 28, RECORD_SIZE)
    struct.pack_into("<Q", record, 32, base)
    struct.pack_into("<I", record, 44, number)

    pos = 0x38
    for attr in attrs or []:
        record[pos:pos + len(attr)] = attr
        pos += len(attr)
    struct.pack_into("<I", record, pos, 0xFFFFFFFF)
    pos += 8
    struct.pack_into("<I", record, 24, pos)

    usn = b"\xA5\x5A"
    record[0x30:0x32] = usn
    record[0x32:0x34] = record[510:512]
    record[0x34:0x36] = record[1022:1024]
    record[510:512] = usn
    record[1022:1024] = usn
    return bytes(record)


def make_image(path: Path) -> None:
    image = bytearray(TOTAL_CLUSTERS * CLUSTER_SIZE)
    boot = memoryview(image)[:BPS]
    boot[0:3] = b"\xEB\x52\x90"
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 11, BPS)
    boot[13] = SPC
    struct.pack_into("<Q", boot, 40, TOTAL_CLUSTERS)
    struct.pack_into("<Q", boot, 48, MFT_LCN)
    struct.pack_into("<Q", boot, 56, 64)
    boot[64] = 0xF6  # -10 -> 1024-byte MFT records
    boot[68] = 1
    boot[510:512] = b"\x55\xAA"

    records = [bytes(RECORD_SIZE) for _ in range(MFT_RECORDS)]
    records[0] = _record(0, attrs=[
        _nonresident_attr(0x80, 0, [(MFT_LCN, MFT_CLUSTERS)], MFT_RECORDS * RECORD_SIZE)
    ])
    records[5] = _record(5, directory=True, attrs=[
        _nonresident_attr(0xA0, 0, [(200, 1), (220, 1)], 2 * CLUSTER_SIZE)
    ])
    records[6] = _record(6, attrs=[
        _nonresident_attr(0x80, 0, [(100, 1)], CLUSTER_SIZE)
    ])
    records[10] = _record(10, attrs=[
        _nonresident_attr(0x80, 0, [(300, 2), (400, 2)], 4 * CLUSTER_SIZE)
    ])
    records[11] = _record(11, attrs=[
        _nonresident_attr(0x80, 0, [(500, 4)], 4 * CLUSTER_SIZE)
    ])
    records[12] = _record(12)
    records[13] = _record(13, attrs=[
        _nonresident_attr(0x80, 0, [(600, 2)], 4 * CLUSTER_SIZE)
    ])
    records[14] = _record(14, base=13, attrs=[
        _nonresident_attr(0x80, 2, [(700, 2)], 4 * CLUSTER_SIZE)
    ])

    mft_offset = MFT_LCN * CLUSTER_SIZE
    for index, record in enumerate(records):
        image[mft_offset + index * RECORD_SIZE:mft_offset + (index + 1) * RECORD_SIZE] = record

    bitmap = bytearray((TOTAL_CLUSTERS + 7) // 8)
    used_clusters = set(range(MFT_LCN, MFT_LCN + MFT_CLUSTERS))
    used_clusters.update({100, 200, 220})
    used_clusters.update(range(300, 302))
    used_clusters.update(range(400, 402))
    used_clusters.update(range(500, 504))
    used_clusters.update(range(600, 602))
    used_clusters.update(range(700, 702))
    for cluster in used_clusters:
        bitmap[cluster >> 3] |= 1 << (cluster & 7)
    image[100 * CLUSTER_SIZE:101 * CLUSTER_SIZE] = bitmap
    path.write_bytes(image)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="linux-defragger-ntfs-") as directory:
        image = Path(directory) / "ntfs.img"
        make_image(image)
        result = BACKEND.map(str(image), 128)
        assert result["filesystem"] == "ntfs"
        assert result["regular_files"] == 6, result
        assert result["directories"] == 1, result
        assert result["fragmented_files"] == 2, result
        assert result["fragmented_directories"] == 1, result
        assert abs(result["fragmentation_percent"] - 33.3333333333) < 0.001, result
        assert result["details"]["fragmentation_available"] is True, result
        assert result["details"]["mft_records_scanned"] == MFT_RECORDS, result
        assert result["details"]["fragmented_clusters_mapped"] == 10, result
        assert result["details"]["directory_clusters_mapped"] == 2, result
        assert sum(cell["fragmented"] for cell in result["cells"]) == 10, result
        assert sum(cell["directory"] for cell in result["cells"]) == 2, result
        assert result["used_bytes"] > 0 and result["free_bytes"] > 0, result
    print("NTFS allocation and MFT fragmentation test passed")


if __name__ == "__main__":
    main()
