#!/usr/bin/python3
import os,struct,sys,math
p=sys.argv[1]; size=64*1024*1024; bps=512; spc=8; cs=bps*spc
fat_off=24; fat_len=128; heap_off=fat_off+fat_len; cc=(size//bps-heap_off)//spc; root=2
img=bytearray(size)
bs=img[:512]; bs[3:11]=b'EXFAT   '; struct.pack_into('<Q',bs,64,0);struct.pack_into('<Q',bs,72,size//bps)
struct.pack_into('<I',bs,80,fat_off);struct.pack_into('<I',bs,84,fat_len);struct.pack_into('<I',bs,88,heap_off);struct.pack_into('<I',bs,92,cc);struct.pack_into('<I',bs,96,root);struct.pack_into('<I',bs,100,0x12345678);struct.pack_into('<H',bs,104,0x100);bs[108]=9;bs[109]=3;bs[110]=1;bs[111]=0x80;bs[510:512]=b'\x55\xaa';img[:512]=bs
fat=memoryview(img)[fat_off*bps:(fat_off+fat_len)*bps]
def fs(c,v):struct.pack_into('<I',fat,c*4,v)
fs(0,0xfffffff8);fs(1,0xffffffff);fs(2,0xffffffff);fs(3,0xffffffff);fs(10,20);fs(20,11);fs(11,0xffffffff)
def off(c):return (heap_off+(c-2)*spc)*bps
# bitmap cluster 3
bm=bytearray(cs)
for c in [2,3,10,11,20]:
 i=c-2;bm[i//8]|=1<<(i%8)
img[off(3):off(3)+cs]=bm
# root entries bitmap + file set
rootb=bytearray(cs)
rootb[0]=0x81;struct.pack_into('<I',rootb,20,3);struct.pack_into('<Q',rootb,24,math.ceil(cc/8))
# file primary at 32
O=32;rootb[O]=0x85;rootb[O+1]=2;struct.pack_into('<H',rootb,O+4,0x20)
S=O+32;rootb[S]=0xC0;rootb[S+1]=0;rootb[S+3]=8;struct.pack_into('<Q',rootb,S+8,3*cs);struct.pack_into('<I',rootb,S+20,10);struct.pack_into('<Q',rootb,S+24,3*cs)
N=O+64;rootb[N]=0xC1;name='TEST.BIN'.encode('utf-16le');rootb[N+2:N+2+len(name)]=name
def csum(buf,o,count):
 s=0
 for i in range(count*32):
  if i in (2,3):continue
  s=((s>>1)|((s&1)<<15));s=(s+buf[o+i])&0xffff
 return s
struct.pack_into('<H',rootb,O+2,csum(rootb,O,3));img[off(2):off(2)+cs]=rootb
for idx,c in enumerate([10,20,11]):img[off(c):off(c)+cs]=bytes([65+idx])*cs
open(p,'wb').write(img)
print(p,cc)
