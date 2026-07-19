from __future__ import annotations
from .base import *

INFO=BackendInfo("minix","Minix Filesystem",("minix","minix2","minix3"),CAP_ANALYSE|CAP_MAP,"summary")
_MAGICS={0x137F:"v1",0x138F:"v1-30char",0x2468:"v2",0x2478:"v2-30char",0x4D5A:"v3"}
class MinixBackend:
    info=INFO
    def _read(self,r:Reader):
        sb=r.read(1024,64)
        vals=[]
        for endian in ("little","big"):
            magic=int.from_bytes(sb[16:18],endian)
            if magic in _MAGICS:return magic,_MAGICS[magic],endian
            magic3=int.from_bytes(sb[24:26],endian)
            if magic3 in _MAGICS:return magic3,_MAGICS[magic3],endian
        raise BackendError("not a recognised Minix filesystem")
    def probe(self,path:str)->bool:
        try:
            with Reader(path) as r:self._read(r)
            return True
        except (OSError,BackendError):return False
    def map(self,path:str,cells:int)->dict:
        with Reader(path) as r:
            magic,variant,endian=self._read(r); unit=1024; total=max(1,(r.size+unit-1)//unit)
            return aggregate_ranges(total,cells,unit,"minix",[(0,total,2)],"summary",{
                "magic":hex(magic),"variant":variant,"byte_order":endian,
                "note":"Minix filesystem detected; zone bitmap location mapping is not yet decoded"
            })
BACKEND=MinixBackend()
