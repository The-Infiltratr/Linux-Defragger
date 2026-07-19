#!/usr/bin/env python3
"""Verify FAT tail-fill compaction without requiring defragmentation."""

import struct
import sys
from pathlib import Path

p = Path(sys.argv[1])
file_count = 32
clusters_per_file = 16
with p.open('rb') as f:
    boot=f.read(512)
    bps=struct.unpack_from('<H',boot,11)[0];spc=boot[13]
    reserved=struct.unpack_from('<H',boot,14)[0];fats=boot[16]
    fatsz=struct.unpack_from('<I',boot,36)[0];root=struct.unpack_from('<I',boot,44)[0]&0x0fffffff
    cs=bps*spc;data_sector=reserved+fats*fatsz
    def coff(c):return (data_sector+(c-2)*spc)*bps
    f.seek(reserved*bps);fat=f.read(fatsz*bps)
    def fatv(c):return struct.unpack_from('<I',fat,c*4)[0]&0x0fffffff
    f.seek(coff(root));entries=[f.read(32) for _ in range(file_count)]
    highest=1;fragmented=0
    for fi,e in enumerate(entries):
        first=((struct.unpack_from('<H',e,20)[0]<<16)|struct.unpack_from('<H',e,26)[0])&0x0fffffff
        chain=[];seen=set();c=first
        while 2<=c<0x0ffffff8:
            assert c not in seen,(fi,'loop',c);seen.add(c);chain.append(c);c=fatv(c)
        assert len(chain)==clusters_per_file,(fi,len(chain))
        fragments=1+sum(b!=a+1 for a,b in zip(chain,chain[1:]))
        if fragments>1:fragmented+=1
        for logical,cluster in enumerate(chain):
            f.seek(coff(cluster));marker=f.read(2)
            assert marker==bytes([fi,logical]),(fi,logical,cluster,marker)
        highest=max(highest,max(chain))
    assert fragmented>0,'Compact unexpectedly defragmented every file'
    for c in range(2,highest+1):
        assert fatv(c)!=0,('internal free cluster',c)
print(f'verified tail-fill compaction: no internal gaps, payloads intact, {fragmented} files remain fragmented')
