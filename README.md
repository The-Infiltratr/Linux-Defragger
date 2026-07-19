# Linux Defragger 1.8.0

Linux Defragger provides graphical allocation maps and filesystem-specific offline mutation engines.

- FAT12, FAT16 and FAT32: analyse, map, compact, defragment and recover.
- exFAT: analyse, map, compact, defragment and recover.
- Amiga OFS/FFS variants: analyse, map, compact, defragment and recover.
- Classic Apple HFS: analyse, map, compact, defragment and recover.
- Apple HFS+ and HFSX: analyse, map, compact, defragment, recover and live map updates.
- NTFS: analyse, map, native offline compact and recover. File-by-file defragmentation is not yet implemented.
- EXT2/3/4 and other listed modern filesystems: read-only allocation and fragmentation analysis where supported.

## Native NTFS compact implementation

NTFS Compact is implemented inside Linux Defragger and has no NTFS-3G runtime dependency. The engine reads the NTFS boot sector, MFT, mapping pairs and `$Bitmap` directly. It conservatively relocates ordinary, unnamed, non-resident file data streams into lower free extents and updates the affected MFT record and allocation bitmap itself.

Each move is an external journalled transaction. The destination is copied first, the volume is marked dirty in `$Volume` and `$MFTMirr`, destination clusters are reserved, mapping pairs are switched, old clusters are released, and the original clean volume flags are restored. Recover inspects the actual on-disk MFT record and either completes the move forward or restores the original record and bitmap bytes.

The planner resolves the owner of the actual allocated high-water cluster before writing. It relocates only the physical extent that owns that boundary, or a suffix of it. When one lower contiguous hole is too small, the logical extent may be split across several lower free extents. Mapping pairs are regenerated in VCN order, and the existing MFT attribute can be expanded inside its current 1 KiB MFT record when sufficient unused record space exists. No `$ATTRIBUTE_LIST` record is created.

After every transaction the engine recalculates the boundary. If the boundary is fixed by an unsupported object such as `$MFTMirr`, `$LogFile`, a directory index, a named/compressed/sparse/encrypted stream, or an `$ATTRIBUTE_LIST` segment, it stops immediately and reports the exact MFT record and attribute instead of relocating unrelated lower files. Progress is based on real high-water reduction toward the theoretical packed boundary.

The current writer deliberately does not move NTFS system files, directories, compressed streams, sparse streams, encrypted streams, named streams, or streams split through `$ATTRIBUTE_LIST`. These remain valid but can limit how far the final allocation high-water mark falls. Per-stream move details are available from the command-line engine with `--diagnostic-log PATH`; the GUI operation log is intentionally summarised.
