#!/usr/bin/python3
from __future__ import annotations
import heapq
import io
import json
import os
import struct
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gui'))

import native_compact_engine as n
from backends.base import CAP_COMPACT
from backends.ext4 import BACKEND as EXT
from backends.btrfs import BACKEND as BTRFS
from backends.xfs import BACKEND as XFS


def test_ioctl_layouts():
    assert n.FS_IOC_FIEMAP == 3223348747
    assert n.FS_IOC_FSGETXATTR == 2149341215
    assert n.XFS_IOC_EXCHANGE_RANGE == 1076385921
    assert n.EXT4_IOC_MOVE_EXT == 3223873039
    assert n.BTRFS_IOC_RESIZE == 1342215171
    captured = {}
    original = n.fcntl.ioctl
    def fake_ioctl(fd, request_code, request, mutate=True):
        captured['fd'] = fd
        captured['request_code'] = request_code
        captured['amount'] = bytes(request[8:]).split(b'\0', 1)[0]
        return 0
    try:
        n.fcntl.ioctl = fake_ioctl
        n._btrfs_resize(17, 7, 123456789)
    finally:
        n.fcntl.ioctl = original
    assert captured == {
        'fd': 17,
        'request_code': n.BTRFS_IOC_RESIZE,
        'amount': b'7:123456789',
    }



def test_native_compact_argument_parser_is_present_and_accepts_gui_abi():
    args = n.parse_args([
        'compact', '/dev/test', '--write', '--confirm', '/dev/test',
        '--journal', '/run/test.journal', '--filesystem', 'ext4',
        '--ram-buffer', 'auto', '--workers', 'auto', '--live-map-cells', '262080',
    ])
    assert args.operation == 'compact'
    assert args.device == '/dev/test'
    assert args.filesystem == 'ext4'
    assert args.write is True
    assert args.confirm == '/dev/test'
    assert args.live_map_cells == 262080

def test_tail_source_selection_and_partial_suffix():
    heap = []
    low = n.SourceExtent('/low', 0, 100, 50, 0, 0)
    high = n.SourceExtent('/high', 4096, 1000, 200, 0, 1)
    heapq.heappush(heap, (-low.physical_end, low.token, low.generation, low))
    heapq.heappush(heap, (-high.physical_end, high.token, high.generation, high))
    selected = n._pop_high_source(heap, 300)
    assert selected is high
    selected.length -= 64
    n._requeue_source(heap, selected)
    selected2 = n._pop_high_source(heap, 300)
    assert selected2 is high
    assert selected2.physical_end == 1136


def test_range_merging():
    result = n._merge_ranges([(20, 30), (0, 10), (8, 15), (40, 40)])
    assert [(x.start, x.end) for x in result] == [(0, 15), (20, 30)]


def test_copy_range_zero_pads_last_allocated_block():
    with tempfile.TemporaryDirectory() as directory:
        source_path = Path(directory) / 'source'
        donor_path = Path(directory) / 'donor'
        source_path.write_bytes(b'abc')
        donor_path.write_bytes(b'\0' * 8)
        source_fd = os.open(source_path, os.O_RDONLY)
        donor_fd = os.open(donor_path, os.O_RDWR)
        try:
            n._copy_range(source_fd, 0, donor_fd, 8)
        finally:
            os.close(source_fd)
            os.close(donor_fd)
        assert donor_path.read_bytes() == b'abc' + b'\0' * 5


def test_ext2_and_ext3_do_not_advertise_compact_in_gui():
    gui = (ROOT / 'gui' / 'linux_defragger_gui.py').read_text()
    assert 'self.fstype.lower() != "ext4"' in gui
    assert 'capabilities &= ~CAP_COMPACT' in gui


def test_backend_capabilities():
    assert EXT.info.capabilities & CAP_COMPACT
    assert BTRFS.info.capabilities & CAP_COMPACT
    assert XFS.info.capabilities & CAP_COMPACT


def test_gui_and_helper_dispatch_are_wired():
    gui = (ROOT / 'gui' / 'linux_defragger_gui.py').read_text()
    helper = (ROOT / 'gui' / 'privileged_helper.py').read_text()
    assert 'find_native_compact_engine' in gui
    assert 'native-compact-engine' in gui
    assert 'volume.normalized_fstype in {"ext4", "btrfs", "xfs"}' in gui
    assert 'NATIVE_COMPACT_ENGINE' in helper
    assert 'native-compact-engine' in helper


def test_collector_uses_total_free_blocks_including_privileged_reserve():
    collector = object.__new__(n.SpaceCollector)
    collector.block_size = 4096
    collector.mountpoint = '/fake'
    collector._new_unlinked_file = lambda _prefix: 7
    allocations = []
    collector._fallocate = lambda fd, mode, offset, length: allocations.append(
        (fd, mode, offset, length)
    )

    class Stats:
        def __init__(self, free, available):
            self.f_bfree = free
            self.f_bavail = available
            self.f_frsize = 4096

    responses = iter((Stats(10, 2), Stats(1, 1)))
    original_statvfs = n.os.statvfs
    original_fsync = n.os.fsync
    original_floor = n.COLLECTOR_FLOOR
    try:
        n.os.statvfs = lambda _path: next(responses)
        n.os.fsync = lambda _fd: None
        n.COLLECTOR_FLOOR = 4096
        allocated, transactions = collector.fill_available()
    finally:
        n.os.statvfs = original_statvfs
        n.os.fsync = original_fsync
        n.COLLECTOR_FLOOR = original_floor

    assert allocated == 9 * 4096
    assert transactions == 1
    assert allocations == [(7, 0, 0, 9 * 4096)]

def test_collector_extents_keep_fd_and_logical_offsets():
    collector = object.__new__(n.SpaceCollector)
    collector.fds = [17]
    collector.block_size = 4096
    original = n.fiemap
    try:
        n.fiemap = lambda fd, start=0, length=(1 << 64) - 1, batch=512: [
            n.FiemapExtent(logical=8192, physical=16384, length=12288, flags=0)
        ]
        extents = collector.owned_extents(1 << 30)
    finally:
        n.fiemap = original
    assert len(extents) == 1
    item = extents[0]
    assert (item.fd, item.logical, item.physical, item.length) == (17, 8192, 16384, 12288)


def test_collector_slice_verification_uses_existing_mapping():
    collector = object.__new__(n.SpaceCollector)
    collector.block_size = 4096
    item = n.CollectorExtent(fd=19, logical=8192, physical=32768, length=16384, flags=0)
    original = n.fiemap
    try:
        n.fiemap = lambda fd, start=0, length=(1 << 64) - 1, batch=512: [
            n.FiemapExtent(logical=8192, physical=32768, length=16384, flags=0)
        ]
        collector.verify_slice(item, 12288, 36864, 4096)
    finally:
        n.fiemap = original


def test_copy_range_supports_nonzero_donor_offset():
    with tempfile.TemporaryDirectory() as directory:
        source_path = Path(directory) / 'source-offset'
        donor_path = Path(directory) / 'donor-offset'
        source_path.write_bytes(b'abcdefgh')
        donor_path.write_bytes(b'_' * 20)
        source_fd = os.open(source_path, os.O_RDONLY)
        donor_fd = os.open(donor_path, os.O_RDWR)
        try:
            n._copy_range(source_fd, 2, donor_fd, 4, 8)
        finally:
            os.close(source_fd)
            os.close(donor_fd)
        assert donor_path.read_bytes() == b'_' * 8 + b'cdef' + b'_' * 8


def test_ext4_exchange_uses_collector_logical_offset():
    original = n.fcntl.ioctl
    captured = {}
    def fake_ioctl(fd, request_code, request, mutate=True):
        values = list(struct.unpack('=IIQQQQ', request))
        captured['values'] = values[:]
        values[5] = values[4]
        request[:] = struct.pack('=IIQQQQ', *values)
        return 0
    try:
        n.fcntl.ioctl = fake_ioctl
        n._ext4_exchange(3, 4, 8192, 16384, 4096, 4096)
    finally:
        n.fcntl.ioctl = original
    assert captured['values'][2:5] == [2, 4, 1]


def test_xfs_exchange_uses_collector_logical_offset():
    original = n.fcntl.ioctl
    captured = {}
    def fake_ioctl(fd, request_code, request):
        captured['values'] = struct.unpack('=iIQQQQ', request)
        return 0
    try:
        n.fcntl.ioctl = fake_ioctl
        n._xfs_exchange(3, 4, 8192, 16384, 4096)
    finally:
        n.fcntl.ioctl = original
    assert captured['values'][2:5] == (16384, 8192, 4096)


def test_extent_compactor_does_not_allocate_second_donor_file():
    source = (ROOT / 'gui' / 'native_compact_engine.py').read_text()
    assert 'allocate_donor' not in source
    assert 'punch_physical' not in source
    assert 'collector.verify_slice' in source
    assert '_copy_range(source_fd, source_offset, gap.fd, move, donor_logical)' in source


def test_live_range_event_contains_physical_move():
    output = io.StringIO()
    with redirect_stdout(output):
        n._emit_live_range(8192, 4096, 4096, 16384, 2, 1000)
    line = output.getvalue().strip()
    assert line.startswith('@@LIVE_RANGE ')
    data = json.loads(line.split(' ', 1)[1])
    assert data == {
        'source_start_byte': 8192,
        'destination_start_byte': 4096,
        'length_bytes': 4096,
        'moved_total_bytes': 16384,
        'pass': 2,
    }


def test_extent_compactor_repeats_until_fixed_point_and_gui_applies_live_ranges():
    engine = (ROOT / 'gui' / 'native_compact_engine.py').read_text()
    gui = (ROOT / 'gui' / 'linux_defragger_gui.py').read_text()
    assert 'MAX_EXTENT_COMPACT_PASSES = 32' in engine
    assert 'for pass_number in range(1, MAX_EXTENT_COMPACT_PASSES + 1)' in engine
    assert 'if result.moved_bytes <= 0:' in engine
    assert '@@LIVE_RANGE ' in engine
    assert 'range_prefix = "@@LIVE_RANGE "' in gui
    assert 'fragmentation recalculated at completion' in gui


def test_btrfs_compact_uses_balance_then_resize():
    source = (ROOT / 'gui' / 'native_compact_engine.py').read_text()
    assert 'BTRFS_IOC_RESIZE' in source
    assert 'BTRFS_IOC_BALANCE_V2' in source
    assert '_run_btrfs_balance' in source
    assert 'balance-and-shrink repack' in source
    targets = n._candidate_shrink_targets(15 * 1024**3, 1024**3, 22 * 1024**3)
    assert targets and all(15 * 1024**3 < value < 22 * 1024**3 for value in targets)
    assert targets == sorted(targets, reverse=True)
    request = n._btrfs_balance_request(7, 100)
    assert len(request) == 1024
    assert struct.unpack_from('=Q', request, 0)[0] == (
        n.BTRFS_BALANCE_DATA | n.BTRFS_BALANCE_METADATA
    )
    for offset in (n.BTRFS_BALANCE_DATA_OFFSET, n.BTRFS_BALANCE_META_OFFSET):
        assert struct.unpack_from('=Q', request, offset + 8)[0] == 100
        assert struct.unpack_from('=Q', request, offset + 16)[0] == 7
        assert struct.unpack_from('=Q', request, offset + 64)[0] == (
            n.BTRFS_BALANCE_ARGS_USAGE | n.BTRFS_BALANCE_ARGS_DEVID
        )



def test_btrfs_balance_worker_reports_progress():
    original_ioctl = n.fcntl.ioctl
    original_stop = n._stop_requested
    progress_calls = 0

    def fake_ioctl(fd, request_code, request, mutate=True):
        nonlocal progress_calls
        assert fd == 17
        if request_code == n.BTRFS_IOC_BALANCE_V2:
            time.sleep(0.08)
            return 0
        if request_code == n.BTRFS_IOC_BALANCE_PROGRESS:
            progress_calls += 1
            struct.pack_into('=QQQ', request, n.BTRFS_BALANCE_PROGRESS_OFFSET, 4, 4, min(4, progress_calls))
            return 0
        raise AssertionError(hex(request_code))

    try:
        n.fcntl.ioctl = fake_ioctl
        n._stop_requested = False
        output = io.StringIO()
        with redirect_stdout(output):
            assert n._run_btrfs_balance(17, 1, 1)
    finally:
        n.fcntl.ioctl = original_ioctl
        n._stop_requested = original_stop
    assert progress_calls >= 1
    assert 'Btrfs data/metadata balance completed.' in output.getvalue()



def test_ext4_compact_iterates_shrink_and_regular_file_packing():
    state = {'blocks': 1000}
    commands = []
    original_assert = n._assert_unmounted
    original_which = n.shutil.which
    original_geometry = n.ext4_compact_geometry
    original_run = n._run_ext4_tool
    original_extent = n._run_extent_compaction
    original_stop = n._stop_requested
    try:
        n._assert_unmounted = lambda _device: None
        n.shutil.which = lambda name: f'/usr/sbin/{name}'
        n.ext4_compact_geometry = lambda _device: (4096, state['blocks'] * 4096)
        def fake_run(command, accepted=None):
            commands.append((tuple(command), accepted))
            if command[0].endswith('resize2fs') and '-M' in command:
                state['blocks'] = 420
            elif command[0].endswith('resize2fs') and command[-1] == '1000':
                state['blocks'] = 1000
            return 0
        n._run_ext4_tool = fake_run
        n._run_extent_compaction = lambda *_args, **_kwargs: n.ExtentCompactSummary()
        n._stop_requested = False
        output = io.StringIO()
        with redirect_stdout(output):
            assert n.compact_ext4_offline('/dev/test') == 0
    finally:
        n._assert_unmounted = original_assert
        n.shutil.which = original_which
        n.ext4_compact_geometry = original_geometry
        n._run_ext4_tool = original_run
        n._run_extent_compaction = original_extent
        n._stop_requested = original_stop
    assert state['blocks'] == 1000
    assert [item[0] for item in commands] == [
        ('/usr/sbin/e2fsck', '-f', '-D', '-p', '/dev/test'),
        ('/usr/sbin/resize2fs', '-M', '-p', '/dev/test'),
        ('/usr/sbin/resize2fs', '-p', '/dev/test', '1000'),
        ('/usr/sbin/e2fsck', '-f', '-n', '/dev/test'),
    ]
    report = output.getvalue()
    assert 'iterative offline filesystem-wide repack' in report
    assert 'reached a fixed point after round 1' in report
    assert 'fit below block 419' in report


if __name__ == '__main__':
    for name, value in sorted(globals().items()):
        if name.startswith('test_') and callable(value):
            value()
            print(name, 'ok')
