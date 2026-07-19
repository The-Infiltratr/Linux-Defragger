from __future__ import annotations
from .base import *

INFO = BackendInfo("ntfs", "NTFS", ("ntfs", "ntfs3"), CAP_ANALYSE|CAP_MAP, "exact")


def _signed_le(data: bytes) -> int:
    if not data:
        return 0
    return int.from_bytes(data, "little", signed=True)


def _runlist(data: bytes):
    pos = 0
    lcn = 0
    while pos < len(data):
        header = data[pos]
        pos += 1
        if header == 0:
            return
        lsz = header & 0x0F
        osz = header >> 4
        if not lsz or pos + lsz + osz > len(data):
            raise BackendError("invalid NTFS runlist")
        length = int.from_bytes(data[pos:pos+lsz], "little")
        pos += lsz
        delta = _signed_le(data[pos:pos+osz]) if osz else None
        pos += osz
        if delta is None:
            yield None, length
        else:
            lcn += delta
            yield lcn, length


def _fixup(record: bytearray, sector_size: int) -> None:
    usa_off = u16le(record, 4)
    usa_count = u16le(record, 6)
    if usa_count < 1 or usa_off + usa_count * 2 > len(record):
        raise BackendError("invalid NTFS update sequence array")
    usn = record[usa_off:usa_off+2]
    for i in range(1, usa_count):
        end = i * sector_size
        if end > len(record) or record[end-2:end] != usn:
            raise BackendError("NTFS MFT fixup mismatch")
        record[end-2:end] = record[usa_off+i*2:usa_off+i*2+2]

class NtfsBackend:
    info = INFO

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            return r.read(3, 8) == b"NTFS    "

    def map(self, path: str, cells: int) -> dict:
        with Reader(path) as r:
            bs = r.read(0, 512)
            if bs[3:11] != b"NTFS    ":
                raise BackendError("not an NTFS volume")
            bps = u16le(bs, 11)
            spc_raw = int.from_bytes(bs[13:14], "little", signed=True)
            if spc_raw <= 0:
                raise BackendError("unsupported NTFS sectors-per-cluster encoding")
            cluster_size = bps * spc_raw
            total_sectors = u64le(bs, 40)
            total_clusters = total_sectors // spc_raw
            mft_lcn = u64le(bs, 48)
            rec_raw = int.from_bytes(bs[64:65], "little", signed=True)
            record_size = (1 << -rec_raw) if rec_raw < 0 else rec_raw * cluster_size
            if record_size < 512 or record_size > 1024 * 1024:
                raise BackendError("invalid NTFS MFT record size")
            # The first MFT records are guaranteed to be addressable from the MFT start in normal volumes.
            raw = bytearray(r.read(mft_lcn * cluster_size + 6 * record_size, record_size))
            if raw[:4] != b"FILE":
                raise BackendError("NTFS $Bitmap MFT record not found at record 6")
            _fixup(raw, bps)
            attr_off = u16le(raw, 20)
            bitmap_runs = None
            data_size = None
            pos = attr_off
            while pos + 16 <= len(raw):
                atype = u32le(raw, pos)
                if atype == 0xFFFFFFFF:
                    break
                alen = u32le(raw, pos + 4)
                if alen < 24 or pos + alen > len(raw):
                    raise BackendError("invalid NTFS attribute")
                nonresident = raw[pos + 8]
                name_len = raw[pos + 9]
                if atype == 0x80 and name_len == 0:
                    if nonresident:
                        run_off = u16le(raw, pos + 32)
                        data_size = u64le(raw, pos + 48)
                        bitmap_runs = list(_runlist(bytes(raw[pos+run_off:pos+alen])))
                    else:
                        value_len = u32le(raw, pos + 16)
                        value_off = u16le(raw, pos + 20)
                        bitmap = bytes(raw[pos+value_off:pos+value_off+value_len])
                        return aggregate_bitmap(bitmap, total_clusters, cells, cluster_size, "ntfs",
                                                details={"cluster_size": cluster_size})
                    break
                pos += alen
            if bitmap_runs is None or data_size is None:
                raise BackendError("NTFS $Bitmap data attribute not found")
            bitmap = bytearray()
            for lcn, length in bitmap_runs:
                byte_len = length * cluster_size
                if lcn is None:
                    bitmap += b"\x00" * byte_len
                else:
                    bitmap += r.read(lcn * cluster_size, byte_len)
                if len(bitmap) >= data_size:
                    break
            bitmap = bitmap[:data_size]
            if len(bitmap) * 8 < total_clusters:
                raise BackendError("NTFS $Bitmap is shorter than the volume")
            return aggregate_bitmap(bytes(bitmap), total_clusters, cells, cluster_size, "ntfs",
                                    details={"cluster_size": cluster_size})

BACKEND = NtfsBackend()
