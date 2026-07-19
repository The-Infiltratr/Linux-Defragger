#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Journalled native exFAT compaction, defragmentation and recovery.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Native exFAT relocation, journalling and recovery engine."""

from __future__ import annotations
import argparse, json, math, os, signal, struct, sys, tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

EOC=0xFFFFFFFF
STOP=False

def sigint(_s,_f):
    global STOP; STOP=True
signal.signal(signal.SIGINT,sigint)

def u16(b,o): return struct.unpack_from('<H',b,o)[0]
def u32(b,o): return struct.unpack_from('<I',b,o)[0]
def u64(b,o): return struct.unpack_from('<Q',b,o)[0]
def p16(b,o,v): struct.pack_into('<H',b,o,v)
def p32(b,o,v): struct.pack_into('<I',b,o,v)

class Error(RuntimeError): pass

@dataclass
class Entry:
    path:str; is_dir:bool; first:int; length:int; valid_length:int; nofat:bool
    parent_clusters:list[int]; entry_off:int; entry_count:int; clusters:list[int]

class Volume:
    def __init__(self,path,writable=False):
        flags=os.O_RDWR if writable else os.O_RDONLY
        self.path=path; self.fd=os.open(path,flags|getattr(os,'O_CLOEXEC',0)); self.writable=writable
        bs=self.read(0,512)
        if bs[3:11]!=b'EXFAT   ': raise Error('not an exFAT volume')
        self.bps=1<<bs[108]; self.spc=1<<bs[109]; self.cs=self.bps*self.spc
        self.fat_off=u32(bs,80); self.fat_len=u32(bs,84); self.heap_off=u32(bs,88)
        self.cc=u32(bs,92); self.root=u32(bs,96); self.serial=u32(bs,100)
        self.volume_flags=u16(bs,106)
        self.number_of_fats=bs[110]
        if self.number_of_fats not in (1,2): raise Error('unsupported exFAT FAT count')
        self.active_fat=(self.volume_flags & 1) if self.number_of_fats==2 else 0
        if self.volume_flags & 0x0006:
            raise Error('exFAT volume is dirty or reports media failure')
        self.active_fat_offset=(self.fat_off+self.active_fat*self.fat_len)*self.bps
        self.fat=bytearray(self.read(self.active_fat_offset,self.fat_len*self.bps))
        self.bitmap_cluster,self.bitmap_length=self._find_bitmap()
        self.bitmap_clusters=self.chain(self.bitmap_cluster, math.ceil(self.bitmap_length/self.cs), allow_contig=False)
        self.bitmap=bytearray(self.read_stream(self.bitmap_clusters,self.bitmap_length))
    def close(self): os.close(self.fd)
    def read(self,o,n):
        d=os.pread(self.fd,n,o)
        if len(d)!=n: raise Error(f'short read at {o}')
        return d
    def write(self,o,d):
        if not self.writable: raise Error('read-only handle')
        n=os.pwrite(self.fd,d,o)
        if n!=len(d): raise Error(f'short write at {o}')
    def sync(self): os.fsync(self.fd)
    def coff(self,c):
        if c<2 or c>=self.cc+2: raise Error(f'invalid cluster {c}')
        return (self.heap_off+(c-2)*self.spc)*self.bps
    def fatget(self,c): return u32(self.fat,c*4)
    def fatset(self,c,v): p32(self.fat,c*4,v)
    def bit(self,c):
        i=c-2; return (self.bitmap[i>>3]>>(i&7))&1
    def setbit(self,c,val):
        i=c-2; m=1<<(i&7)
        if val:self.bitmap[i>>3]|=m
        else:self.bitmap[i>>3]&=~m
    def chain(self,first,count=None,allow_contig=False):
        if first==0:return []
        if allow_contig and count is not None:return list(range(first,first+count))
        out=[]; seen=set(); c=first
        while 2<=c<0xFFFFFFF8:
            if c in seen:raise Error('FAT loop')
            seen.add(c);out.append(c)
            if count is not None and len(out)>=count:break
            c=self.fatget(c)
        if count is not None and len(out)!=count:raise Error('short FAT chain')
        return out
    def read_stream(self,clusters,length):
        out=bytearray()
        for c in clusters: out+=self.read(self.coff(c),self.cs)
        return bytes(out[:length])
    def write_stream(self,clusters,data):
        pos=0
        for c in clusters:
            chunk=data[pos:pos+self.cs]
            if len(chunk)<self.cs: chunk+=b'\0'*(self.cs-len(chunk))
            self.write(self.coff(c),chunk);pos+=self.cs
    def _root_bytes(self):
        cl=self.chain(self.root)
        return cl,bytearray(self.read_stream(cl,len(cl)*self.cs))
    def _find_bitmap(self):
        cl,data=self._root_bytes()
        for o in range(0,len(data),32):
            t=data[o]
            if t==0:return (_ for _ in ()).throw(Error('allocation bitmap not found'))
            if t==0x81:return u32(data,o+20),u64(data,o+24)
        raise Error('allocation bitmap not found')
    def flush_fat_bitmap(self):
        self.write(self.active_fat_offset,self.fat)
        self.write_stream(self.bitmap_clusters,self.bitmap)
    def free_run(self,n,prefer_low=True,exclude=set()):
        rng=range(2,self.cc+2) if prefer_low else range(self.cc+1,1,-1)
        start=None;run=0;prev=None
        for c in rng:
            ok=(not self.bit(c)) and c not in exclude
            contiguous= prev is None or (c==prev+1 if prefer_low else c==prev-1)
            if ok and contiguous:
                if start is None:start=c
                run+=1
                if run>=n:return start if prefer_low else c
            else:start=None;run=0
            prev=c
        return None
    def parse(self):
        out=[]; visited=set()
        def walk(path,first,length,nofat):
            if first in visited:return
            visited.add(first)
            cnt=math.ceil(length/self.cs) if length else 1
            clusters=self.chain(first,cnt,allow_contig=nofat)
            data=bytearray(self.read_stream(clusters,len(clusters)*self.cs))
            o=0
            while o+32<=len(data):
                t=data[o]
                if t==0:break
                if t==0x85:
                    sc=data[o+1]; total=1+sc
                    if o+total*32>len(data):raise Error('truncated directory entry set')
                    stored=u16(data,o+2)
                    if stored != checksum_set(data,o,total):
                        raise Error(f"bad exFAT directory-set checksum at {path or '/'}:{o}")
                    attrs=u16(data,o+4); stream=None; names=[]
                    for j in range(1,total):
                        e=data[o+j*32:o+(j+1)*32]
                        if e[0]==0xC0:stream=e
                        elif e[0]==0xC1:names.append(e[2:32])
                    if stream is not None:
                        nl=stream[3]
                        raw=b''.join(names)[:nl*2]
                        name=raw.decode('utf-16le','replace') or '<unnamed>'
                        fc=u32(stream,20); valid=u64(stream,8); dl=u64(stream,24)
                        nf=bool(stream[1]&2); isdir=bool(attrs&0x10)
                        count=math.ceil(dl/self.cs) if dl else 0
                        fcl=self.chain(fc,count,allow_contig=nf) if count else []
                        p=f'{path}/{name}' if path else name
                        ent=Entry(p,isdir,fc,dl,valid,nf,list(clusters),o,total,fcl);out.append(ent)
                        if isdir and fc and dl:walk(p,fc,dl,nf)
                    o+=total*32;continue
                o+=32
        rootcl=self.chain(self.root)
        walk('',self.root,len(rootcl)*self.cs,False)
        return out

def checksum_set(buf,off,count):
    s=0
    for i in range(count*32):
        if i in (2,3):continue
        s=((s>>1)|((s&1)<<15));s=(s+buf[off+i])&0xFFFF
    return s

def update_entry(v:Volume,e:Entry,new_first:int,new_nofat=True):
    data=bytearray(v.read_stream(e.parent_clusters,len(e.parent_clusters)*v.cs))
    stream_off=None
    for j in range(1,e.entry_count):
        if data[e.entry_off+j*32]==0xC0:stream_off=e.entry_off+j*32;break
    if stream_off is None:raise Error('stream extension missing')
    p32(data,stream_off+20,new_first)
    if new_nofat:data[stream_off+1]|=2
    else:data[stream_off+1]&=~2
    p16(data,e.entry_off+2,checksum_set(data,e.entry_off,e.entry_count))
    v.write_stream(e.parent_clusters,data)

def journal_write(path,obj):
    tmp=path+'.tmp'
    with open(tmp,'w') as f:json.dump(obj,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    d=os.open(str(Path(path).parent),os.O_RDONLY);os.fsync(d);os.close(d)

def recover(device,journal):
    if mounted(device): raise Error('refusing to recover a mounted exFAT volume')
    if not os.path.exists(journal):raise Error('no recovery journal')
    j=json.load(open(journal));v=Volume(device,True)
    if int(j.get('serial',-1)) != v.serial:
        v.close(); raise Error('journal does not match this exFAT volume')
    try:
        entries={e.path:e for e in v.parse()};e=entries.get(j['path'])
        if e is None:raise Error('journal file entry not found')
        src=j['src'];dst=j['dst'];phase=j['phase']
        # The directory entry is the commit point.  Inspect it instead of
        # trusting that the final journal phase reached stable storage.
        if e.first == dst[0]:
            for i,c in enumerate(dst):v.setbit(c,True);v.fatset(c,dst[i+1] if i+1<len(dst) else EOC)
            update_entry(v,e,dst[0],True)
            for c in src:v.setbit(c,False);v.fatset(c,0)
            v.flush_fat_bitmap();v.sync()
        elif e.first == src[0]:
            for c in dst:v.setbit(c,False);v.fatset(c,0)
            v.flush_fat_bitmap();v.sync()
        else:
            raise Error('journal entry points to neither source nor destination')
        os.unlink(journal)
        print('Recovery completed.')
    finally:v.close()

def move_one(v,e,dst,journal):
    src=e.clusters; obj={'schema':1,'device':v.path,'serial':v.serial,'path':e.path,'src':src,'dst':dst,'phase':'prepared'}
    journal_write(journal,obj)
    for s,d in zip(src,dst):v.write(v.coff(d),v.read(v.coff(s),v.cs))
    v.sync();obj['phase']='copied';journal_write(journal,obj)
    for i,c in enumerate(dst):v.setbit(c,True);v.fatset(c,dst[i+1] if i+1<len(dst) else EOC)
    v.flush_fat_bitmap();v.sync();obj['phase']='destination-ready';journal_write(journal,obj)
    obj['phase']='switching';journal_write(journal,obj)
    update_entry(v,e,dst[0],True);v.sync();obj['phase']='switched';journal_write(journal,obj)
    for c in src:v.setbit(c,False);v.fatset(c,0)
    v.flush_fat_bitmap();v.sync();os.unlink(journal)

def mounted(path):
    real=os.path.realpath(path)
    try:
        with open('/proc/self/mountinfo','r',encoding='utf-8',errors='replace') as fh:
            for line in fh:
                fields=line.rstrip().split(' - ',1)
                if len(fields)!=2:
                    continue
                tail=fields[1].split()
                src=tail[1] if len(tail)>1 else ''
                if src and os.path.realpath(src)==real:
                    return True
    except OSError:
        pass
    return False

def fragments(clusters):
    if not clusters: return 0
    return 1 + sum(1 for a,b in zip(clusters,clusters[1:]) if b != a+1)

def command(device,op,journal,max_files=None):
    if mounted(device): raise Error('refusing to modify a mounted exFAT volume')
    if os.path.exists(journal):raise Error('unfinished journal exists; run recover')
    v=Volume(device,True)
    try:
        moved=0
        while not STOP:
            entries=[e for e in v.parse() if e.clusters]
            selected=None; selected_dst=None
            if op=='defrag':
                candidates=[e for e in entries if fragments(e.clusters)>1]
                candidates.sort(key=lambda e:(-fragments(e.clusters),-len(e.clusters),e.path))
                for e in candidates:
                    low=v.free_run(len(e.clusters),True,set(e.clusters))
                    if low is not None:
                        selected=e;selected_dst=list(range(low,low+len(e.clusters)));break
            else:
                candidates=sorted(entries,key=lambda e:(min(e.clusters),e.path))
                for e in candidates:
                    low=v.free_run(len(e.clusters),True,set(e.clusters))
                    if low is not None and low < min(e.clusters):
                        selected=e;selected_dst=list(range(low,low+len(e.clusters)));break
            if selected is None: break
            kind='DIR' if selected.is_dir else 'FILE'
            print(f'move: {kind} {selected.path} ({len(selected.clusters)} clusters, {fragments(selected.clusters)} fragments) -> cluster {selected_dst[0]}',flush=True)
            move_one(v,selected,selected_dst,journal);moved+=1
            # Refresh in-memory FAT/bitmap and directory locations after each committed move.
            v.close();v=Volume(device,True)
            if max_files and moved>=max_files:break
        print(f'Relocated {moved} exFAT objects.',flush=True)
    finally:v.close()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('operation',choices=['defrag','compact','recover']);ap.add_argument('device')
    ap.add_argument('--write',action='store_true');ap.add_argument('--confirm');ap.add_argument('--journal',required=True);ap.add_argument('--max-files',type=int)
    ap.add_argument('--ram-buffer');ap.add_argument('--workers');ap.add_argument('--live-map-cells');ap.add_argument('--transaction-files')
    a=ap.parse_args()
    if not a.write or a.confirm!=a.device:raise Error('write confirmation required')
    if a.operation=='recover':recover(a.device,a.journal)
    else:command(a.device,a.operation,a.journal,a.max_files)
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except Error as e:print(f'exfat-engine: {e}',file=sys.stderr);raise SystemExit(1)
