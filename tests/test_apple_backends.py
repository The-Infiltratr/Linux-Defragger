import os, struct, json, sys, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'gui'))
sys.path.insert(0,str(root/'vendor'))
from backends.registry import Registry

out=Path(tempfile.mkdtemp(prefix='linux-defragger-apple-'))
# Classic HFS: 100 allocation blocks of 4096, bitmap at sector 3.
hfs=out/'hfs.img'; data=bytearray(1024*1024)
m=1024; data[m:m+2]=b'BD'; struct.pack_into('>H',data,m+14,3); struct.pack_into('>H',data,m+18,100); struct.pack_into('>I',data,m+20,4096); struct.pack_into('>H',data,m+34,90)
# allocated blocks 0..9, MSB first
for i in range(10): data[3*512+i//8] |= 1 << (7-(i%8))
hfs.write_bytes(data)
# HFS+ 256 blocks of 4096; allocation bitmap in block 2.
hp=out/'hfsplus.img'; data=bytearray(4096*256); h=1024; data[h:h+2]=b'H+'; struct.pack_into('>H',data,h+2,4); struct.pack_into('>I',data,h+40,4096); struct.pack_into('>I',data,h+44,256); struct.pack_into('>I',data,h+48,240); struct.pack_into('>I',data,h+32,0); struct.pack_into('>I',data,h+36,0)
# allocation fork: logical size 32, total blocks1, extent block2 count1
struct.pack_into('>Q',data,h+112,32); struct.pack_into('>I',data,h+124,1); struct.pack_into('>II',data,h+128,2,1)
for i in range(16): data[2*4096+i//8] |= 1 << (7-(i%8))
hp.write_bytes(data)
# APFS summary
ap=out/'apfs.img'; data=bytearray(4096*64); data[32:36]=b'NXSB'; struct.pack_into('<I',data,36,4096); struct.pack_into('<Q',data,40,64); data[72:88]=bytes(range(16)); ap.write_bytes(data)
reg=Registry()
for path,fs in [(hfs,'hfs'),(hp,'hfsplus'),(ap,'apfs')]:
 b=reg.by_fstype(fs); assert b and b.probe(str(path)); result=b.map(str(path),64); print(fs,result['total_bytes'],result['used_bytes'],result['free_bytes'],result['map_accuracy'])
assert reg.by_fstype('hfsx').info.id=='hfsplus'
print('PASS')
