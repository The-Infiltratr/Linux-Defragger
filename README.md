# Linux Defragger 1.8.0-18

Linux Defragger provides graphical allocation maps, fragmentation analysis, offline free-space compaction, file defragmentation, FAT growth-space layouts and journalled recovery for supported filesystems.

The user-visible operations are deliberately separate:

- **Analyse** reads filesystem metadata and reports allocation, free space and fragmentation. It is read-only and may run against a mounted volume as a live snapshot.
- **Compact** fills internal free-space gaps so free space moves toward the physical end of the volume. It is not a file-defragmentation command.
- **Defragment** rebuilds fragmented files into contiguous allocation runs. It does not attempt to pack every free-space gap.
- **Growth Defrag** is a FAT12/16/32-only layout operation. It first compacts the FAT allocation, then rebuilds allocated objects in physical order and deliberately leaves free expansion room after each regular file.

## FAT Growth Defrag

The GTK **Growth Defrag** button requests a 10 percent reserve after every non-empty regular file. The native FAT engine also accepts `--growth-percent 1..25` for direct command-line use.

The reserve is measured in complete FAT clusters. Each file receives `ceil(file_clusters × percentage / 100)` free clusters, so a very small file receives a one-cluster minimum at the default 10 percent. Directories remain contiguous but do not receive a growth gap.

Growth Defrag works in two durable phases:

1. Compact the FAT allocation so the terminal free area can be used as an overlap-safe workspace.
2. Preserve the current physical object order, rebuild each file or directory as one contiguous chain, and insert the requested free gap after each regular file.

Objects are placed from the end of the plan backwards. When a destination overlaps the object's current allocation, the complete object is first moved into the reusable terminal workspace and then placed at its final location. Every move uses the normal external mapped-cluster journal, so Recover can finish an interrupted transaction.

Growth Defrag uses RAM-backed multi-object batches. Several complete files and directories are read into the relocation cache, written sequentially, and committed through one journal and one grouped metadata update. On systems with substantial free memory, automatic mode preserves 8 GiB for Linux and can use up to 16 GiB as the cache budget; an individual Growth Defrag transaction is capped at 4 GiB to keep journal size and safe-stop latency bounded. SD/eMMC targets use two ordered read workers automatically, while rotational media use one.

Before either phase begins, Growth Defrag performs an idempotence preflight. If every allocated FAT object is already contiguous and every regular file already has at least the requested free reserve immediately after it, the engine reports **Not needed** and performs no compaction, relocation, FAT update or filesystem write. Extra free space after a file is accepted; it is not destroyed merely to reproduce an exact percentage.

Normal GUI output reports batch summaries rather than every filename. Direct command-line users can request object-level detail with `--verbose` or write it to a file with `--diagnostic-log PATH`. FAT long-file-name records are decoded from UTF-16 to UTF-8 for these reports.

The operation refuses to start unless the volume has enough free clusters for both the requested growth gaps and a workspace at least as large as the largest allocated FAT object. It never silently reduces the requested percentage.

## Filesystem support

- FAT12, FAT16 and FAT32: analyse, map, compact, defragment, Growth Defrag and recover.
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

### NTFS Defragment

NTFS Defragment finds supported ordinary files that have more than one physical extent. Each file is copied in logical order into the highest suitable contiguous free run anywhere on the volume. Freed source holes are not reused during the same defragmentation pass, keeping Defragment separate from Compact.

### NTFS recovery and current limits

Every NTFS move uses an external journal. Data is copied first, destination clusters are reserved, the MFT record is switched, old clusters are released, and the original clean volume flags are restored. Recover inspects the actual on-disk MFT record and completes the transaction forward or restores the original record and bitmap state.

The current native writer deliberately leaves NTFS system files and directories, named data streams, compressed/sparse/encrypted streams, `$ATTRIBUTE_LIST` extension streams and malformed MFT records unchanged.

## Interface

The title bar, engine/GUI build label and About dialog show the complete package revision. The File menu provides image opening, volume refresh and Quit. The About menu provides program, author, version and project information. The Growth Defrag button is enabled only when the selected backend advertises the FAT growth-layout capability.

## Safe stopping

A Stop request finishes the active journalled transaction and exits between complete transactions or complete Growth Defrag objects. The engine returns status `130` for this deliberate safe stop. The GTK interface treats that status as **Stopped safely**, refreshes the allocation map and does not claim that the interrupted operation completed.

If Growth Defrag is stopped during its preparation compaction, no growth-space layout has begun and no expansion gaps have been applied. If it is stopped during layout, only the complete objects reported in the summary have been repositioned and the requested reserve layout is explicitly marked partial.
