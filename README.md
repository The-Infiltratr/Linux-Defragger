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

NTFS Compact is implemented inside Linux Defragger and has no NTFS-3G runtime dependency. The engine reads the NTFS boot sector, MFT, mapping pairs and `$Bitmap` directly. It conservatively relocates ordinary, unnamed, non-resident file data streams into lower contiguous free runs and updates the affected MFT record and allocation bitmap itself.

Each file move is an external journalled transaction. The destination is copied first, the NTFS dirty bit is temporarily added in `$Volume` and `$MFTMirr`, destination clusters are reserved, mapping pairs are switched, old clusters are released, and the exact original volume flags are restored. Non-dirty flags such as the observed undocumented `0x0080` bit are preserved throughout the transaction. Recover inspects the actual on-disk MFT record and either completes the move forward or restores the original record and bitmap bytes.

The first native implementation deliberately does not move NTFS system files, directories, compressed streams, sparse streams, encrypted streams, named streams, or streams split through `$ATTRIBUTE_LIST`. These remain valid but can limit how far the final allocation high-water mark falls.
