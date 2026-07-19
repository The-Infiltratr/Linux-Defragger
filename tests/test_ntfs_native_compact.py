#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Verify native NTFS compact, defragment, bitmap updates and recovery.

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gui"))
import ntfs_engine

BPS = 512
SPC = 1
CLUSTER_SIZE = 512
RECORD_SIZE = 1024
TOTAL_CLUSTERS = 4096
MFT_LCN = 4
MFT_RECORDS = 32
MFT_CLUSTERS = MFT_RECORDS * RECORD_SIZE // CLUSTER_SIZE
BITMAP_LCN = 100
DATA_LCN = 3500
DATA_CLUSTERS = 16
BLOCKER_LCN = 3800
MIRROR_CLUSTERS = 4 * RECORD_SIZE // CLUSTER_SIZE


def runlist(runs: list[tuple[int, int]]) -> bytes:
    output = bytearray()
    previous = 0
    for lcn, length in runs:
        length_bytes = length.to_bytes(max(1, (length.bit_length() + 7) // 8), "little")
        delta = lcn - previous
        for size in range(1, 9):
            try:
                offset_bytes = delta.to_bytes(size, "little", signed=True)
            except OverflowError:
                continue
            if int.from_bytes(offset_bytes, "little", signed=True) == delta:
                break
        output.append((len(offset_bytes) << 4) | len(length_bytes))
        output += length_bytes + offset_bytes
        previous = lcn
    output.append(0)
    return bytes(output)


def nonresident(atype: int, runs: list[tuple[int, int]], data_size: int,
                mapping_slack: int = 0) -> bytes:
    mapping = runlist(runs)
    length = (64 + len(mapping) + mapping_slack + 7) & ~7
    attr = bytearray(length)
    struct.pack_into("<I", attr, 0, atype)
    struct.pack_into("<I", attr, 4, length)
    attr[8] = 1
    clusters = sum(size for _lcn, size in runs)
    struct.pack_into("<Q", attr, 16, 0)
    struct.pack_into("<Q", attr, 24, clusters - 1)
    struct.pack_into("<H", attr, 32, 64)
    struct.pack_into("<Q", attr, 40, clusters * CLUSTER_SIZE)
    struct.pack_into("<Q", attr, 48, data_size)
    struct.pack_into("<Q", attr, 56, data_size)
    attr[64:64 + len(mapping)] = mapping
    return bytes(attr)


def resident(atype: int, value: bytes) -> bytes:
    length = (24 + len(value) + 7) & ~7
    attr = bytearray(length)
    struct.pack_into("<I", attr, 0, atype)
    struct.pack_into("<I", attr, 4, length)
    struct.pack_into("<I", attr, 16, len(value))
    struct.pack_into("<H", attr, 20, 24)
    attr[24:24 + len(value)] = value
    return bytes(attr)


def record(number: int, attrs: list[bytes]) -> bytes:
    fixed = bytearray(RECORD_SIZE)
    fixed[:4] = b"FILE"
    struct.pack_into("<H", fixed, 4, 0x30)
    struct.pack_into("<H", fixed, 6, 3)
    struct.pack_into("<H", fixed, 16, 1)
    struct.pack_into("<H", fixed, 18, 1)
    struct.pack_into("<H", fixed, 20, 0x38)
    struct.pack_into("<H", fixed, 22, 1)
    struct.pack_into("<I", fixed, 28, RECORD_SIZE)
    struct.pack_into("<I", fixed, 44, number)
    pos = 0x38
    for attr in attrs:
        fixed[pos:pos + len(attr)] = attr
        pos += len(attr)
    struct.pack_into("<I", fixed, pos, 0xFFFFFFFF)
    pos += 8
    struct.pack_into("<I", fixed, 24, pos)
    # The engine's writer is used here so the synthetic record follows exactly
    # the same update-sequence layout as a real on-disk record.
    fixed[0x30:0x36] = b"\x01\x00\x00\x00\x00\x00"
    return ntfs_engine._prepare_fixups(fixed, BPS)


def make_image(path: Path, volume_flags: int = 0, high_mftmirr_blocker: bool = False,
               split_destinations: bool = False,
               fragmented_data: bool = False,
               occupied_tail: bool = False) -> bytes:
    image = bytearray(TOTAL_CLUSTERS * CLUSTER_SIZE)
    boot = memoryview(image)[:BPS]
    boot[0:3] = b"\xeb\x52\x90"
    boot[3:11] = b"NTFS    "
    struct.pack_into("<H", boot, 11, BPS)
    boot[13] = SPC
    struct.pack_into("<Q", boot, 40, TOTAL_CLUSTERS)
    struct.pack_into("<Q", boot, 48, MFT_LCN)
    mirror_lcn = BLOCKER_LCN if high_mftmirr_blocker else 3000
    struct.pack_into("<Q", boot, 56, mirror_lcn)
    boot[64] = 0xF6
    boot[68] = 1
    boot[72:80] = bytes.fromhex("0123456789abcdef")
    boot[510:512] = b"\x55\xaa"

    records = [bytes(RECORD_SIZE) for _ in range(MFT_RECORDS)]
    records[0] = record(0, [nonresident(0x80, [(MFT_LCN, MFT_CLUSTERS)], MFT_RECORDS * RECORD_SIZE)])
    volume_info = bytearray(12)
    volume_info[8] = 3
    volume_info[9] = 1
    struct.pack_into("<H", volume_info, 10, volume_flags)
    if high_mftmirr_blocker:
        records[1] = record(1, [nonresident(0x80, [(mirror_lcn, MIRROR_CLUSTERS)], 4 * RECORD_SIZE)])
    records[3] = record(3, [resident(0x70, bytes(volume_info))])
    records[6] = record(6, [nonresident(0x80, [(BITMAP_LCN, 1)], (TOTAL_CLUSTERS + 7) // 8)])
    data_runs = ([(DATA_LCN, DATA_CLUSTERS // 2),
                  (DATA_LCN + 32, DATA_CLUSTERS // 2)]
                 if fragmented_data else [(DATA_LCN, DATA_CLUSTERS)])
    records[24] = record(24, [nonresident(
        0x80, data_runs, DATA_CLUSTERS * CLUSTER_SIZE,
        mapping_slack=0,
    )])
    mft_offset = MFT_LCN * CLUSTER_SIZE
    for number, raw in enumerate(records):
        image[mft_offset + number * RECORD_SIZE:mft_offset + (number + 1) * RECORD_SIZE] = raw
    mirror_offset = mirror_lcn * CLUSTER_SIZE
    for number in range(4):
        image[mirror_offset + number * RECORD_SIZE:mirror_offset + (number + 1) * RECORD_SIZE] = records[number]

    bitmap = bytearray((TOTAL_CLUSTERS + 7) // 8)
    # Reserve the boot area independently so Compact does not encounter an
    # artificial three-cluster hole before the synthetic MFT.
    used = set(range(0, MFT_LCN + MFT_CLUSTERS))
    used.add(BITMAP_LCN)
    for lcn, length in data_runs:
        used.update(range(lcn, lcn + length))
    if split_destinations:
        holes = set(range(200, 204)) | set(range(500, 505)) | set(range(1000, 1007))
        used.update(cluster for cluster in range(DATA_LCN) if cluster not in holes)
    if high_mftmirr_blocker:
        used.update(range(mirror_lcn, mirror_lcn + MIRROR_CLUSTERS))
    if occupied_tail:
        # Simulate a volume whose physical tail is completely occupied while
        # large internal free runs still exist. Defragment must use those
        # internal destinations rather than incorrectly requiring tail space.
        used.update(range(DATA_LCN + 64, TOTAL_CLUSTERS - 1))
    for cluster in used:
        bitmap[cluster >> 3] |= 1 << (cluster & 7)
    image[BITMAP_LCN * CLUSTER_SIZE:(BITMAP_LCN + 1) * CLUSTER_SIZE] = bitmap

    payload = bytes((index * 37 + 11) & 0xFF for index in range(DATA_CLUSTERS * CLUSTER_SIZE))
    offset = 0
    for lcn, length in data_runs:
        count = length * CLUSTER_SIZE
        image[lcn * CLUSTER_SIZE:(lcn + length) * CLUSTER_SIZE] = payload[offset:offset + count]
        offset += count
    path.write_bytes(image)
    return payload


def current_volume_flags(path: Path) -> int:
    volume = ntfs_engine._open_volume(str(path), False)
    try:
        _raw0, _fixed0, mft_attr = ntfs_engine._record_zero(volume)
        _offset, _raw, fixed = ntfs_engine._read_mft_record(volume, mft_attr.runs, 3)
        for attr in ntfs_engine._attributes(fixed):
            if attr.atype == ntfs_engine.ATTR_VOLUME_INFORMATION and not attr.nonresident:
                value_off = ntfs_engine._u16(fixed, attr.offset + 20)
                return ntfs_engine._u16(fixed, attr.offset + value_off + 10)
        raise AssertionError("volume information missing")
    finally:
        ntfs_engine._close_volume(volume)


def current_data_runs(path: Path) -> tuple[ntfs_engine.Run, ...]:
    volume = ntfs_engine._open_volume(str(path), False)
    try:
        layout = ntfs_engine._read_layout(volume)
        _offset, _raw, fixed = ntfs_engine._read_mft_record(volume, layout.mft_runs, 24)
        attrs = [attr for attr in ntfs_engine._attributes(fixed)
                 if attr.atype == ntfs_engine.ATTR_DATA and attr.nonresident and not attr.name]
        assert len(attrs) == 1
        return attrs[0].runs
    finally:
        ntfs_engine._close_volume(volume)


def current_data_run(path: Path) -> tuple[int, int]:
    runs = current_data_runs(path)
    assert len(runs) == 1 and runs[0].lcn is not None
    return int(runs[0].lcn), runs[0].length


def current_data_payload(path: Path, length: int) -> bytes:
    runs = current_data_runs(path)
    volume = ntfs_engine._open_volume(str(path), False)
    try:
        return ntfs_engine._read_stream(volume, runs, 0, length)
    finally:
        ntfs_engine._close_volume(volume)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="linux-defragger-native-ntfs-") as directory:
        tmp = Path(directory)
        image = tmp / "volume.img"
        journal = tmp / "native.journal"
        payload = make_image(image)
        expected = hashlib.sha256(payload).hexdigest()
        assert current_data_run(image) == (DATA_LCN, DATA_CLUSTERS)
        ntfs_engine._stop_requested = False
        assert ntfs_engine.compact(str(image), journal) == 0
        assert not journal.exists()
        runs = current_data_runs(image)
        assert sum(run.length for run in runs) == DATA_CLUSTERS
        assert all(run.lcn is not None and run.lcn < DATA_LCN for run in runs)
        actual = current_data_payload(image, len(payload))
        assert hashlib.sha256(actual).hexdigest() == expected
        raw = image.read_bytes()
        bitmap = raw[BITMAP_LCN * CLUSTER_SIZE:(BITMAP_LCN + 1) * CLUSTER_SIZE]
        assert all(
            bitmap[c >> 3] & (1 << (c & 7))
            for run in runs for c in range(int(run.lcn), int(run.lcn) + run.length)
        )
        assert all(not (bitmap[c >> 3] & (1 << (c & 7))) for c in range(DATA_LCN, DATA_LCN + DATA_CLUSTERS))

        # Compact must not split a file extent across several holes. That would
        # alter fragmentation and belongs to Defragment, not Compact.
        payload = make_image(image, split_destinations=True)
        expected = hashlib.sha256(payload).hexdigest()
        ntfs_engine._stop_requested = False
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            assert ntfs_engine.compact(str(image), journal) == 0
        runs = current_data_runs(image)
        assert runs == (ntfs_engine.Run(DATA_LCN, DATA_CLUSTERS),)
        volume = ntfs_engine._open_volume(str(image), False)
        try:
            actual = ntfs_engine._read_stream(volume, runs, 0, len(payload))
        finally:
            ntfs_engine._close_volume(volume)
        assert hashlib.sha256(actual).hexdigest() == expected
        assert "without changing file fragmentation" in captured.getvalue()

        # Compact may move a complete fragment into a lower hole, but the file
        # must retain the same number of physical fragments.
        payload = make_image(image, fragmented_data=True)
        expected = hashlib.sha256(payload).hexdigest()
        before_runs = current_data_runs(image)
        assert ntfs_engine._physical_fragment_count(before_runs) == 2
        ntfs_engine._stop_requested = False
        assert ntfs_engine.compact(str(image), journal) == 0
        after_runs = current_data_runs(image)
        assert after_runs != before_runs
        assert ntfs_engine._physical_fragment_count(after_runs) == 2
        assert hashlib.sha256(current_data_payload(image, len(payload))).hexdigest() == expected

        # Defragment rebuilds that same two-fragment file as one contiguous run
        # in the highest suitable free area on the volume.
        payload = make_image(image, fragmented_data=True)
        expected = hashlib.sha256(payload).hexdigest()
        ntfs_engine._stop_requested = False
        assert ntfs_engine.defragment(str(image), journal) == 0
        defragged_runs = current_data_runs(image)
        assert ntfs_engine._physical_fragment_count(defragged_runs) == 1
        assert defragged_runs[0].lcn is not None
        assert int(defragged_runs[0].lcn) > DATA_LCN + 32
        assert hashlib.sha256(current_data_payload(image, len(payload))).hexdigest() == expected

        # A full physical tail must not disable Defragment. The engine should
        # choose the highest suitable internal free run and still rebuild the
        # stream as one extent without turning the pass into Compact.
        payload = make_image(image, fragmented_data=True, occupied_tail=True)
        expected = hashlib.sha256(payload).hexdigest()
        before_runs = current_data_runs(image)
        assert ntfs_engine._physical_fragment_count(before_runs) == 2
        captured = io.StringIO()
        ntfs_engine._stop_requested = False
        with contextlib.redirect_stdout(captured):
            assert ntfs_engine.defragment(str(image), journal) == 0
        defragged_runs = current_data_runs(image)
        assert ntfs_engine._physical_fragment_count(defragged_runs) == 1
        assert defragged_runs[0].lcn is not None
        assert int(defragged_runs[0].lcn) < TOTAL_CLUSTERS - 1
        assert defragged_runs != before_runs
        assert hashlib.sha256(current_data_payload(image, len(payload))).hexdigest() == expected
        report = captured.getvalue()
        assert "highest suitable internal free runs" in report
        assert "rebuilt 1 file streams" in report

        # An immovable high object must no longer prevent unrelated movable
        # data from filling lower gaps.  The boundary remains fixed by
        # $MFTMirr, but the packed prefix advances and the payload is intact.
        payload = make_image(image, high_mftmirr_blocker=True)
        before_runs = current_data_runs(image)
        captured = io.StringIO()
        ntfs_engine._stop_requested = False
        with contextlib.redirect_stdout(captured):
            assert ntfs_engine.compact(str(image), journal) == 0
        after_runs = current_data_runs(image)
        assert after_runs != before_runs
        assert hashlib.sha256(current_data_payload(image, len(payload))).hexdigest() == hashlib.sha256(payload).hexdigest()
        report = captured.getvalue()
        assert "used 1 file streams" in report
        assert "Allocated high-water mark: cluster 3,807 -> 3,807" in report
        assert "Lowest internal free gap advanced" in report

        # Simulate a crash after metadata switched but before old clusters were
        # released. Recovery must complete forward and retain the payload.
        payload = make_image(image)
        volume = ntfs_engine._open_volume(str(image), True)
        try:
            layout = ntfs_engine._read_layout(volume)
            candidate = ntfs_engine._candidate_records(layout)[0]
            destination = ntfs_engine._find_free_run(layout.bitmap, candidate.clusters, candidate.lowest_lcn)
            assert destination is not None
            new_record = ntfs_engine._updated_record(candidate, destination, BPS)
            old_clusters = [c for run in candidate.attribute.runs for c in range(run.lcn, run.lcn + run.length)]
            new_clusters = list(range(destination, destination + candidate.clusters))
            snapshots = ntfs_engine._bitmap_patches(layout, old_clusters + new_clusters)
            ntfs_engine._copy_runs(volume, candidate.attribute.runs, destination, candidate.clusters)
            volume_records = ntfs_engine._volume_record_state(layout)
            state = ntfs_engine._journal_state(layout, candidate, destination, new_record, snapshots,
                                                volume_records, "metadata-switched")
            ntfs_engine._write_journal(journal, state)
            ntfs_engine._write_volume_records(layout, volume_records[1], volume_records[3])
            for cluster in new_clusters:
                ntfs_engine._set_bit(layout.bitmap, cluster, True)
            ntfs_engine._write_bitmap_patches(layout, ntfs_engine._current_bitmap_patches(layout, snapshots))
            ntfs_engine._write_mft_record(volume, layout.mft_runs, candidate.record_number, new_record)
            os.fsync(volume.fd)
        finally:
            ntfs_engine._close_volume(volume)
        assert ntfs_engine.recover(str(image), journal) == 0
        actual = current_data_payload(image, len(payload))
        assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(payload).hexdigest()

        # Schema-3 recovery must also complete a partial move whose destination
        # is represented by several physical extents.
        payload = make_image(image, split_destinations=True)
        volume = ntfs_engine._open_volume(str(image), True)
        try:
            layout = ntfs_engine._read_layout(volume)
            candidate = ntfs_engine._candidate_records(layout)[0]
            high = ntfs_engine._highest_used(layout.bitmap, TOTAL_CLUSTERS)
            move, reason = ntfs_engine._plan_extent_move(layout, candidate, high)
            assert move is not None, reason
            assert len(move.destination_runs) == 3
            new_record = ntfs_engine._updated_record_runs(candidate, move.new_runs, BPS)
            old_clusters = [c for run in move.source_runs
                            for c in range(int(run.lcn), int(run.lcn) + run.length)]
            new_clusters = [c for run in move.destination_runs
                            for c in range(int(run.lcn), int(run.lcn) + run.length)]
            snapshots = ntfs_engine._bitmap_patches(layout, old_clusters + new_clusters)
            ntfs_engine._copy_run_sequences(volume, move.source_runs, move.destination_runs)
            volume_records = ntfs_engine._volume_record_state(layout)
            state = ntfs_engine._journal_state(
                layout, candidate, int(move.destination_runs[0].lcn), new_record,
                snapshots, volume_records, "metadata-switched",
                released_runs=move.source_runs, destination_runs=move.destination_runs,
            )
            ntfs_engine._write_journal(journal, state)
            ntfs_engine._write_volume_records(layout, volume_records[1], volume_records[3])
            for cluster in new_clusters:
                ntfs_engine._set_bit(layout.bitmap, cluster, True)
            ntfs_engine._write_bitmap_patches(
                layout, ntfs_engine._current_bitmap_patches(layout, snapshots)
            )
            ntfs_engine._write_mft_record(volume, layout.mft_runs, candidate.record_number, new_record)
            os.fsync(volume.fd)
        finally:
            ntfs_engine._close_volume(volume)
        assert ntfs_engine.recover(str(image), journal) == 0
        runs = current_data_runs(image)
        volume = ntfs_engine._open_volume(str(image), False)
        try:
            actual = ntfs_engine._read_stream(volume, runs, 0, len(payload))
        finally:
            ntfs_engine._close_volume(volume)
        assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(payload).hexdigest()

        # The same schema-3 multi-extent transaction must roll back when the
        # destination was reserved but the MFT mapping had not switched yet.
        payload = make_image(image, split_destinations=True)
        volume = ntfs_engine._open_volume(str(image), True)
        try:
            layout = ntfs_engine._read_layout(volume)
            candidate = ntfs_engine._candidate_records(layout)[0]
            high = ntfs_engine._highest_used(layout.bitmap, TOTAL_CLUSTERS)
            move, reason = ntfs_engine._plan_extent_move(layout, candidate, high)
            assert move is not None, reason
            new_record = ntfs_engine._updated_record_runs(candidate, move.new_runs, BPS)
            old_clusters = [c for run in move.source_runs
                            for c in range(int(run.lcn), int(run.lcn) + run.length)]
            new_clusters = [c for run in move.destination_runs
                            for c in range(int(run.lcn), int(run.lcn) + run.length)]
            snapshots = ntfs_engine._bitmap_patches(layout, old_clusters + new_clusters)
            ntfs_engine._copy_run_sequences(volume, move.source_runs, move.destination_runs)
            volume_records = ntfs_engine._volume_record_state(layout)
            state = ntfs_engine._journal_state(
                layout, candidate, int(move.destination_runs[0].lcn), new_record,
                snapshots, volume_records, "destination-allocated",
                released_runs=move.source_runs, destination_runs=move.destination_runs,
            )
            ntfs_engine._write_journal(journal, state)
            ntfs_engine._write_volume_records(layout, volume_records[1], volume_records[3])
            for cluster in new_clusters:
                ntfs_engine._set_bit(layout.bitmap, cluster, True)
            ntfs_engine._write_bitmap_patches(
                layout, ntfs_engine._current_bitmap_patches(layout, snapshots)
            )
            os.fsync(volume.fd)
        finally:
            ntfs_engine._close_volume(volume)
        assert ntfs_engine.recover(str(image), journal) == 0
        assert current_data_run(image) == (DATA_LCN, DATA_CLUSTERS)
        original = image.read_bytes()[
            DATA_LCN * CLUSTER_SIZE:(DATA_LCN + DATA_CLUSTERS) * CLUSTER_SIZE
        ]
        assert hashlib.sha256(original).hexdigest() == hashlib.sha256(payload).hexdigest()

        # A real-world NTFS volume may carry the undocumented 0x0080 bit.  It
        # is not the dirty bit and must survive our temporary dirty-state
        # transaction unchanged.
        payload = make_image(image, ntfs_engine.VOLUME_OBSERVED_UNKNOWN_0080)
        assert current_volume_flags(image) == 0x0080
        ntfs_engine._stop_requested = False
        assert ntfs_engine.compact(str(image), journal) == 0
        assert current_volume_flags(image) == 0x0080
        actual = current_data_payload(image, len(payload))
        assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(payload).hexdigest()

        # The actual dirty bit is still a hard stop, and an unrecognised flag
        # remains rejected rather than guessed at.
        make_image(image, ntfs_engine.VOLUME_IS_DIRTY)
        try:
            ntfs_engine.compact(str(image), journal)
        except ntfs_engine.NtfsCompactError as exc:
            assert "dirty flag" in str(exc)
        else:
            raise AssertionError("dirty NTFS volume was accepted")
        make_image(image, 0x0040)
        try:
            ntfs_engine.compact(str(image), journal)
        except ntfs_engine.NtfsCompactError as exc:
            assert "unsupported NTFS volume flags" in str(exc)
        else:
            raise AssertionError("unknown NTFS volume flag was accepted")

    print("Native NTFS compact/defragment, recovery and volume-flag tests passed")


if __name__ == "__main__":
    main()
