# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

from .base import *

class FatBackend:
    def __init__(self, fat_bits: int):
        self.fat_bits = fat_bits
        self.info = BackendInfo(
            id=f"fat{fat_bits}", display_name=f"FAT{fat_bits}",
            aliases=(f"fat{fat_bits}", "vfat" if fat_bits == 32 else f"msdos{fat_bits}"),
            capabilities=CAP_ANALYSE|CAP_MAP|CAP_COMPACT|CAP_DEFRAG|CAP_RECOVER|CAP_LIVE_MAP|CAP_GROWTH_DEFRAG,
            map_accuracy="exact")

    def probe(self, path: str) -> bool:
        with Reader(path) as r:
            bs = r.read(0, 512)
        bps = u16le(bs, 11)
        spc = bs[13]
        reserved = u16le(bs, 14)
        fats = bs[16]
        root_entries = u16le(bs, 17)
        total = u16le(bs, 19) or u32le(bs, 32)
        fat_secs = u16le(bs, 22) or u32le(bs, 36)
        if not (bps and spc and reserved and fats and total and fat_secs):
            return False
        root_secs = ((root_entries * 32) + bps - 1) // bps
        data_secs = total - (reserved + fats * fat_secs + root_secs)
        clusters = data_secs // spc
        detected = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
        return detected == self.fat_bits

    def map(self, path: str, cells: int) -> dict:
        raise BackendError("FAT mapping is provided by the native FAT engine")
