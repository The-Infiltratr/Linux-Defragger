#!/usr/bin/env python3
import struct, sys
from pathlib import Path
p=Path(sys.argv[1]); active=int(sys.argv[2]) if len(sys.argv)>2 else 0
with p.open('rb') as f:
    b=f.read(512); bps=struct.unpack_from('<H',b,11)[0]; spc=b[13]
    reserved=struct.unpack_from('<H',b,14)[0]; fats=b[16]; fatsz=struct.unpack_from('<I',b,36)[0]
    data=reserved+fats*fatsz; cs=bps*spc
    def co(c): return data*bps+(c-2)*cs
    f.seek(co(2)); e=f.read(32)
    first=(struct.unpack_from('<H',e,20)[0]<<16)|struct.unpack_from('<H',e,26)[0]
    f.seek((reserved+active*fatsz)*bps); fat=f.read(fatsz*bps)
    def fv(c): return struct.unpack_from('<I',fat,c*4)[0]&0x0fffffff
    chain=[]; c=first
    while True:
        chain.append(c); n=fv(c)
        if n>=0x0ffffff8: break
        c=n
    assert chain==[5,10,6], chain
print('verified original fragmented chain')
