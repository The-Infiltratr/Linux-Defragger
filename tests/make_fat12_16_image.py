#!/usr/bin/env python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

import struct,sys
from pathlib import Path
kind=sys.argv[1]; out=Path(sys.argv[2]); mode=sys.argv[3] if len(sys.argv)>3 else 'fragmented'
if kind=='fat12':
    bps=512; spc=1; reserved=1; fats=2; root_entries=224; total=2880; spf=9; media=0xF0
elif kind=='fat16':
    bps=512; spc=1; reserved=1; fats=2; root_entries=512; total=64000; spf=250; media=0xF8
else: raise SystemExit()
root_secs=(root_entries*32+bps-1)//bps
data_start=reserved+fats*spf+root_secs
clusters=(total-data_start)//spc
img=bytearray(total*bps)
boot=memoryview(img)[:bps]
boot[0:3]=b'\xeb\x3c\x90'; boot[3:11]=b'MSDOS5.0'
struct.pack_into('<H',boot,11,bps); boot[13]=spc; struct.pack_into('<H',boot,14,reserved); boot[16]=fats
struct.pack_into('<H',boot,17,root_entries)
if total<65536: struct.pack_into('<H',boot,19,total)
else: struct.pack_into('<I',boot,32,total)
boot[21]=media; struct.pack_into('<H',boot,22,spf); struct.pack_into('<H',boot,24,18); struct.pack_into('<H',boot,26,2)
boot[36]=0x80; boot[38]=0x29; struct.pack_into('<I',boot,39,0x1234ABCD); boot[43:54]=b'FRAGTEST   '; boot[54:62]=(b'FAT12   ' if kind=='fat12' else b'FAT16   ')
boot[510:512]=b'\x55\xaa'
fat_bytes=spf*bps
fat=bytearray(fat_bytes)
chain=[10,11,12] if mode=='compact' else [5,10,6]
eoc=(0xFFF if kind=='fat12' else 0xFFFF)
entries={chain[i]:(chain[i+1] if i+1<len(chain) else eoc) for i in range(len(chain))}
if kind=='fat12':
    vals={0:0xFF0|media,1:0xFFF,**entries}
    for c,v in vals.items():
        off=c+c//2; pair=struct.unpack_from('<H',fat,off)[0]
        if c&1: pair=(pair&0x000F)|((v&0xFFF)<<4)
        else: pair=(pair&0xF000)|(v&0xFFF)
        struct.pack_into('<H',fat,off,pair)
else:
    vals={0:0xFFF8|media,1:0xFFFF,**entries}
    for c,v in vals.items(): struct.pack_into('<H',fat,c*2,v)
for i in range(fats):
    off=(reserved+i*spf)*bps; img[off:off+fat_bytes]=fat
root_off=(reserved+fats*spf)*bps
e=bytearray(32); e[0:11]=b'FILE    BIN'; e[11]=0x20; struct.pack_into('<H',e,26,chain[0]); struct.pack_into('<I',e,28,3*bps*spc)
img[root_off:root_off+32]=e
for c,ch in zip(chain,[b'A',b'B',b'C']):
    off=(data_start+(c-2)*spc)*bps; img[off:off+bps*spc]=ch*(bps*spc)
out.write_bytes(img)
print(kind,clusters,data_start)
