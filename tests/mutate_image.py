#!/usr/bin/env python3
import argparse
import struct
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('image', type=Path)
p.add_argument('operation', choices=['crosslink', 'orphan', 'active-fat1', 'dirty', 'interrupt-rollback', 'interrupt-commit'])
p.add_argument('--journal', type=Path)
p.add_argument('--device-path')
a = p.parse_args()

with a.image.open('r+b') as f:
    boot = bytearray(f.read(512))
    bps = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from('<H', boot, 14)[0]
    fats = boot[16]
    fatsz = struct.unpack_from('<I', boot, 36)[0]
    data_sector = reserved + fats * fatsz
    csize = bps * spc
    volume_id = struct.unpack_from('<I', boot, 67)[0]
    def coff(c): return data_sector * bps + (c - 2) * csize
    def fatoff(copy, c): return (reserved + copy * fatsz) * bps + c * 4
    def getfat(copy, c):
        f.seek(fatoff(copy, c)); return struct.unpack('<I', f.read(4))[0]
    def setfat(copy, c, v):
        f.seek(fatoff(copy, c)); f.write(struct.pack('<I', v))
    def set_dirent_first(entry_offset, c):
        f.seek(entry_offset + 20); f.write(struct.pack('<H', (c >> 16) & 0xffff))
        f.seek(entry_offset + 26); f.write(struct.pack('<H', c & 0xffff))

    root_off = coff(2)
    if a.operation == 'crosslink':
        f.seek(root_off + 32)
        e = bytearray(32)
        e[0:11] = b'OTHER   BIN'
        e[11] = 0x20
        struct.pack_into('<H', e, 26, 5)
        struct.pack_into('<I', e, 28, 3 * bps)
        f.write(e)
        f.write(b'\0')
    elif a.operation == 'orphan':
        for copy in range(fats): setfat(copy, 20, 0x0fffffff)
    elif a.operation == 'active-fat1':
        struct.pack_into('<H', boot, 40, 0x0081)
        f.seek(0); f.write(boot)
        f.seek(6 * bps); f.write(boot)
        # Deliberately make inactive FAT 0 disagree. Active FAT 1 remains valid.
        setfat(0, 5, 0x0fffffff)
        setfat(0, 6, 0)
        setfat(0, 10, 0)
    elif a.operation == 'dirty':
        for copy in range(fats):
            setfat(copy, 1, getfat(copy, 1) & ~0x08000000)
    elif a.operation in ('interrupt-rollback', 'interrupt-commit'):
        if not a.journal or not a.device_path:
            raise SystemExit('--journal and --device-path are required')
        source = [5, 10, 6]
        dest = [7, 8, 9]
        for s, d in zip(source, dest):
            f.seek(coff(s)); data = f.read(csize)
            f.seek(coff(d)); f.write(data)
        for copy in range(fats):
            setfat(copy, 7, 8); setfat(copy, 8, 9); setfat(copy, 9, 0x0fffffff)
        stage = 2
        if a.operation == 'interrupt-commit':
            set_dirent_first(root_off, 7)
            stage = 3
        a.journal.write_text(
            'LINUX-DEFRAGGER-JOURNAL-1\n'
            f'device={a.device_path}\n'
            f'volume_id={volume_id:08x}\n'
            f'stage={stage}\n'
            f'dirent_offset={root_off}\n'
            'old_first=5\n'
            'dest_start=7\n'
            'count=3\n'
            'source=5,10,6\n'
        )
