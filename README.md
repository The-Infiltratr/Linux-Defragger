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

The compaction planner now performs genuine hole filling. It finds the lowest internal free extent, selects a supported physical file extent above that gap, and moves either the whole extent or a safe suffix into the gap. It repeats from the beginning of the volume so the packed prefix advances monotonically and free space is driven toward the physical end. Whole extents are preferred to avoid unnecessary file fragmentation; partial suffix moves are used only when required and only when the regenerated mapping pairs fit the existing MFT record.

An immovable high-water object no longer prevents unrelated movable files from filling lower gaps. If the lowest remaining gap cannot be filled because only NTFS system metadata, directories, named/compressed/sparse/encrypted streams, `$ATTRIBUTE_LIST` segments, or undecodable records remain above it, Compact stops safely and reports the exact gap and reason. The final report states the lowest remaining gap, the number and size of internal gaps, and the actual boundary reduction.

The current writer deliberately does not move NTFS system files, directories, compressed streams, sparse streams, encrypted streams, named streams, or streams split through `$ATTRIBUTE_LIST`. These can leave unavoidable internal gaps until those layouts gain native relocation support. Per-stream move details are available from the command-line engine with `--diagnostic-log PATH`; the GUI operation log is intentionally summarised.
