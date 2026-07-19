from __future__ import annotations
from .base import *

INFO = BackendInfo("zfs", "ZFS/OpenZFS Member", ("zfs", "zfs_member"), CAP_ANALYSE|CAP_MAP, "summary")
UBER_MAGIC_LE = b"\x0c\xb1\xba\x00\x00\x00\x00\x00"
UBER_MAGIC_BE = UBER_MAGIC_LE[::-1]

class ZfsBackend:
    info = INFO
    def _find(self, r: Reader):
        windows=[]
        if r.size:
            windows.extend([(0,min(r.size,4*1024*1024)),(max(0,r.size-4*1024*1024),min(r.size,4*1024*1024))])
        else:
            windows=[(0,4*1024*1024)]
        for off, length in windows:
            if length < 8: continue
            data=r.read(off,length)
            for magic,endian in ((UBER_MAGIC_LE,"little"),(UBER_MAGIC_BE,"big")):
                p=data.find(magic)
                if p>=0: return off+p,endian
        raise BackendError("not a recognised ZFS member")
    def probe(self,path:str)->bool:
        try:
            with Reader(path) as r:self._find(r)
            return True
        except (OSError,BackendError):return False
    def map(self,path:str,cells:int)->dict:
        with Reader(path) as r:
            pos,endian=self._find(r)
            unit=512; total=max(1,(r.size+unit-1)//unit)
            return aggregate_ranges(total,cells,unit,"zfs",[(0,total,2)],"summary",{
                "uberblock_magic_offset":pos,"byte_order":endian,
                "note":"ZFS member detected; exact allocation requires pool-wide metaslab and space-map traversal"
            })
BACKEND=ZfsBackend()
