# Linux Defragger 1.8.0-13

Linux Defragger provides graphical allocation maps, fragmentation analysis, offline free-space compaction, file defragmentation and journalled recovery for supported filesystems.

The three main operations are deliberately distinct:

- **Analyse** reads filesystem metadata and reports allocation, free space and fragmentation. It is read-only and may run against a mounted volume as a live snapshot.
- **Compact** fills internal free-space gaps so free space moves toward the physical end of the volume. It is not a file-defragmentation command.
- **Defragment** rebuilds fragmented files into contiguous allocation runs. It does not attempt to pack all free-space gaps.

## Filesystem support

- FAT12, FAT16 and FAT32: analyse, map, compact, defragment and recover.
- exFAT: analyse, map, compact, defragment and recover.
- Amiga OFS/FFS variants: analyse, map, compact, defragment and recover.
- Classic Apple HFS: analyse, map, compact, defragment and recover.
- Apple HFS+ and HFSX: analyse, map, compact, defragment, recover and live map updates.
- NTFS: analyse, map, native offline compact, native offline defragment and recover.
- EXT2/3/4, Btrfs, XFS, UFS, ZFS, APFS, Minix and swap: read-only allocation and fragmentation analysis where supported by the backend.

## Native NTFS operations

NTFS support is implemented inside Linux Defragger and has no NTFS-3G runtime dependency. The engine reads and writes the NTFS boot geometry, MFT records, mapping pairs, `$Bitmap`, `$Volume` and `$MFTMirr` directly.

### NTFS Compact

NTFS Compact fills the lowest internal free gap using a complete supported physical file extent from a higher location. A compact move is accepted only when it preserves the file's physical fragment count. Compact does not split one extent across several holes and does not join logical neighbouring extents merely to reduce the fragmentation count.

This separation means a small gap may remain when no complete supported extent fits it without changing fragmentation. The operation reports that limitation rather than silently performing defragmentation.

### NTFS Defragment

NTFS Defragment finds supported ordinary files that have more than one physical extent. Each file is copied in logical order into one contiguous free run selected from the trailing free area at the physical end of the volume. The original extents are released only after the new MFT mapping is durable.

Freed source holes are not reused during the same defragmentation pass. This prevents Defragment from becoming an implicit compaction operation.

### NTFS recovery and current limits

Every NTFS move uses an external journal. Data is copied first, destination clusters are reserved, the MFT record is switched, old clusters are released, and the original clean volume flags are restored. Recover inspects the actual on-disk MFT record and completes the transaction forward or restores the original record and bitmap state.

The current native writer deliberately leaves these layouts unchanged:

- NTFS system files and directories;
- named data streams;
- compressed, sparse or encrypted streams;
- streams split through `$ATTRIBUTE_LIST` extension records;
- malformed or undecodable MFT records.

## Interface

The title bar, build label and About dialog show the complete package revision. The File menu provides image opening, volume refresh and Quit. The About menu provides program, author, version and project information.
