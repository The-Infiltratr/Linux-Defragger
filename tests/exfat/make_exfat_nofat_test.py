#!/usr/bin/python3
"""Create a contiguous NoFatChain exFAT file above low free space."""
from __future__ import annotations
import struct
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
subprocess.run([sys.executable,str(ROOT/'tests/exfat/make_exfat_test.py'),sys.argv[1]],check=True,stdout=subprocess.DEVNULL)
sys.path.insert(0,str(ROOT/'gui'))
from exfat_engine import Volume,checksum_set  # noqa: E402

v=Volume(sys.argv[1],True)
try:
    root=v.chain(v.root);data=bytearray(v.read_stream(root,len(root)*v.cs));primary=32;stream=64
    data[stream+1]|=2
    struct.pack_into('<I',data,stream+20,10)
    struct.pack_into('<H',data,primary+2,checksum_set(data,primary,3))
    v.write_stream(root,data)
    for cluster in (10,11,12,20):
        v.setbit(cluster,cluster in (10,11,12));v.fatset(cluster,0)
    for cluster,byte in zip((10,11,12),(b'A',b'B',b'C')):
        v.write(v.coff(cluster),byte*v.cs)
    v.flush_fat_bitmap();v.sync()
finally:v.close()
