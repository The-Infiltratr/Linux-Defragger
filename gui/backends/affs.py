from __future__ import annotations
from .base import *

INFO = BackendInfo("affs", "Amiga OFS/FFS", ("affs", "amiga", "ofs", "ffs", "dostype"), CAP_ANALYSE|CAP_MAP, "summary")

class AffsBackend:
    info=INFO
    def _boot(self,r:Reader):
        b=r.read(0,512)
        if b[:3]!=b"DOS" or b[3]>7:
            raise BackendError("not an Amiga DOS OFS/FFS volume")
        return b[3]
    def probe(self,path:str)->bool:
        try:
            with Reader(path) as r:self._boot(r)
            return True
        except (OSError,BackendError):return False
    def map(self,path:str,cells:int)->dict:
        with Reader(path) as r:
            dostype=self._boot(r)
            unit=512; total=max(1,(r.size+unit-1)//unit)
            names={0:"OFS",1:"FFS",2:"OFS intl",3:"FFS intl",4:"OFS dircache",5:"FFS dircache",6:"OFS longname",7:"FFS longname"}
            return aggregate_ranges(total,cells,unit,"affs",[(0,total,2)],"summary",{
                "dostype":f"DOS\\{dostype}","variant":names.get(dostype,"Amiga DOS"),
                "note":"Amiga OFS/FFS detected; bitmap-block locations are not yet decoded"
            })
BACKEND=AffsBackend()
