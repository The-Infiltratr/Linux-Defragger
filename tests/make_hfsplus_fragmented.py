#!/usr/bin/env python3
import struct, sys
from pathlib import Path
BS=4096; TOTAL=256

def be16(x): return struct.pack('>H',x)
def be32(x): return struct.pack('>I',x)
def be64(x): return struct.pack('>Q',x)
def fork(logical, blocks, extents):
    b=bytearray(80); b[0:8]=be64(logical); b[8:12]=be32(0); b[12:16]=be32(blocks)
    for i,(s,c) in enumerate(extents[:8]): b[16+i*8:24+i*8]=be32(s)+be32(c)
    return bytes(b)
def node_desc(next_,prev,kind,height,nrec):
    return be32(next_)+be32(prev)+struct.pack('>bBHH',kind,height,nrec,0)
def set_offsets(node, starts, free_end):
    vals=starts+[free_end]
    for i,v in enumerate(vals): node[len(node)-2*(i+1):len(node)-2*i]=be16(v)
def btree_header(node_size,total_nodes,root,leaf_count,first_leaf,last_leaf,max_key=520):
    b=bytearray(106)
    struct.pack_into('>HIIIIHHIIHIBBI',b,0,1,root,leaf_count,first_leaf,last_leaf,node_size,max_key,total_nodes,0,0,node_size*2,0,0,0x06)
    return bytes(b)
def cat_key(parent,name):
    u=name.encode('utf-16-be'); n=len(u)//2; payload=be32(parent)+be16(n)+u
    return be16(len(payload))+payload
def folder_record(folder_id,valence=1):
    b=bytearray(88); struct.pack_into('>hH',b,0,1,0); struct.pack_into('>I',b,4,valence); struct.pack_into('>I',b,8,folder_id); return bytes(b)
def thread_record(kind,parent,name):
    u=name.encode('utf-16-be'); b=bytearray(8+2+len(u)); struct.pack_into('>hH',b,0,kind,0); struct.pack_into('>I',b,4,parent); struct.pack_into('>H',b,8,len(u)//2); b[10:]=u; return bytes(b)
def file_record(fid,extents,logical):
    b=bytearray(248); struct.pack_into('>hH',b,0,2,0); struct.pack_into('>I',b,8,fid); b[88:168]=fork(logical,sum(c for _,c in extents),extents); return bytes(b)
def make_btree(records,node_size=BS):
    hdr=bytearray(node_size); hdr[:14]=node_desc(0,0,1,0,3)
    hrec=btree_header(node_size,2,1,len(records),1,1)
    starts=[14,120,248]; hdr[14:14+len(hrec)]=hrec
    hdr[248]=0xC0
    set_offsets(hdr,starts,249)
    leaf=bytearray(node_size); leaf[:14]=node_desc(0,0,-1,1,len(records))
    pos=14; starts=[]
    for key,data in records:
        starts.append(pos); rec=key+data; leaf[pos:pos+len(rec)]=rec; pos+=len(rec)
        if pos&1: pos+=1
    set_offsets(leaf,starts,pos)
    return bytes(hdr+leaf)
def make_empty_btree(node_size=BS):
    hdr=bytearray(node_size); hdr[:14]=node_desc(0,0,1,0,3)
    hrec=btree_header(node_size,1,0,0,0,0,10)
    hdr[14:14+len(hrec)]=hrec; hdr[248]=0x80; set_offsets(hdr,[14,120,248],249); return bytes(hdr)

def main(path):
    data=bytearray(BS*TOTAL)
    extents=[(10,1),(12,1),(14,1)]; fid=16
    records=[
      (cat_key(1,'TEST'), folder_record(2,1)),
      (cat_key(2,''), thread_record(3,1,'TEST')),
      (cat_key(2,'FILE.BIN'), file_record(fid,extents,BS*3)),
      (cat_key(fid,''), thread_record(4,2,'FILE.BIN')),
    ]
    records.sort(key=lambda kv: kv[0][2:])
    data[3*BS:4*BS]=make_empty_btree()
    data[4*BS:6*BS]=make_btree(records)
    for i,(s,c) in enumerate(extents):
        for b in range(c): data[(s+b)*BS:(s+b+1)*BS]=bytes([0x41+i])*BS
    allocated={0,2,3,4,5,10,12,14,TOTAL-1}
    bitmap=bytearray(BS)
    for x in allocated: bitmap[x//8]|=0x80>>(x%8)
    data[2*BS:3*BS]=bitmap
    vh=bytearray(512); vh[0:2]=b'H+'; vh[2:4]=be16(4); vh[4:8]=be32(0x100); vh[8:12]=b'8.10'
    struct.pack_into('>IIIIIIII',vh,32,1,1,BS,TOTAL,TOTAL-len(allocated),20,BS,BS)
    struct.pack_into('>IIQ',vh,64,17,1,0)
    vh[112:192]=fork(BS,1,[(2,1)]); vh[192:272]=fork(BS,1,[(3,1)]); vh[272:352]=fork(BS*2,2,[(4,2)])
    data[1024:1536]=vh; data[len(data)-1024:len(data)-512]=vh
    Path(path).write_bytes(data)
if __name__=='__main__': main(sys.argv[1])
