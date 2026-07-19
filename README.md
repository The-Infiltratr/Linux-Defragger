# Linux Defragger 1.8.0-21

Linux Defragger provides graphical allocation maps, fragmentation analysis, offline free-space compaction, file defragmentation, FAT/exFAT growth-space layouts and journalled recovery for supported filesystems.

The user-visible operations are deliberately separate:

- **Analyse** reads filesystem metadata and reports allocation, free space and fragmentation. It is read-only and may run against a mounted volume as a live snapshot.
- **Compact** fills internal free-space gaps so free space moves toward the physical end of the volume. It is not a file-defragmentation command.
- **Defragment** rebuilds fragmented files into contiguous allocation runs. It does not attempt to pack every free-space gap.
- **Growth Defrag** is a FAT12/16/32 and exFAT layout operation. It verifies the existing layout first, then rebuilds allocated objects in physical order and deliberately leaves free expansion room after each regular file only when work is required.

## FAT and exFAT Growth Defrag

The GTK **Growth Defrag** button requests a 10 percent reserve after every non-empty regular file on FAT and exFAT. The native FAT engine also accepts `--growth-percent 1..25` for direct command-line use.

The reserve is measured in complete FAT clusters. Each file receives `ceil(file_clusters × percentage / 100)` free clusters, so a very small file receives a one-cluster minimum at the default 10 percent. Directories remain contiguous but do not receive a growth gap.

Growth Defrag works in two durable phases:

1. Compact the FAT allocation so the terminal free area can be used as an overlap-safe workspace.
2. Preserve the current physical object order, rebuild each file or directory as one contiguous chain, and insert the requested free gap after each regular file.

Objects are placed from the end of the plan backwards. When a destination overlaps the object's current allocation, the complete object is first moved into the reusable terminal workspace and then placed at its final location. Every move uses the normal external mapped-cluster journal, so Recover can finish an interrupted transaction.

Growth Defrag uses RAM-backed multi-object batches. Several complete files and directories are read into the relocation cache, written sequentially, and committed through one journal and one grouped metadata update. On systems with substantial free memory, automatic mode preserves 8 GiB for Linux and can use up to 16 GiB as the cache budget; an individual Growth Defrag transaction is capped at 4 GiB to keep journal size and safe-stop latency bounded. SD/eMMC targets use two ordered read workers automatically, while rotational media use one.

Before either phase begins, Growth Defrag performs an explicitly logged, read-only idempotence preflight. It reports the engine revision, the number of files and directories checked, and either confirms the existing layout or names the first fragmented object or file with insufficient post-file reserve. A second canonical-layout verifier independently recognises the exact physical pattern produced by an earlier Growth Defrag pass. If the layout is already satisfactory, the engine reports **Not needed** and performs no compaction, relocation, FAT update, journal creation or filesystem write. Extra free space after a file is accepted; it is not destroyed merely to reproduce an exact percentage.

Normal GUI output reports batch summaries rather than every filename. Direct command-line users can request object-level detail with `--verbose` or write it to a file with `--diagnostic-log PATH`. FAT long-file-name records are decoded from UTF-16 to UTF-8 for these reports.

The operation refuses to start unless the volume has enough free clusters for both the requested growth gaps and a workspace at least as large as the largest allocated FAT object. It never silently reduces the requested percentage.

## Filesystem support

- FAT12, FAT16 and FAT32: analyse, map, compact, defragment, Growth Defrag and recover.
- exFAT: analyse, map, compact, defragment, Growth Defrag and recover.
- Amiga OFS/FFS variants: analyse, map, compact, defragment and recover.
- Classic Apple HFS: analyse, map, compact, defragment and recover.
- Apple HFS+ and HFSX: analyse, map, compact, defragment, recover and live map updates.
- NTFS: analyse, map, native offline compact, native offline defragment and recover.
- EXT2/3/4: read-only allocation maps and file/directory fragmentation analysis.
- Btrfs: genuine native read-only physical allocation maps and file-fragmentation analysis for single-device filesystems using non-striped local profiles. The analyser walks the chunk, root, extent and filesystem trees directly. Multi-device and striped RAID profiles remain conservatively unsupported.
- XFS: genuine native read-only physical allocation maps and file/directory fragmentation analysis. The analyser walks allocation-group free-space B+trees, inode B+trees and inode data-fork extent trees directly. Realtime-file data on a separate realtime device is reported but cannot be positioned on the data-device map.
- UFS, ZFS, APFS, Minix and swap: read-only allocation and fragmentation analysis where supported by the backend.

### FAT and exFAT Compact semantics

FAT and exFAT Compact are intentionally pure compactors. They fill the lowest free clusters with movable allocation copied from the physical tail. They do not prefer complete files and do not attempt to reduce fragmentation; a file may become more fragmented as the price of removing internal gaps. FAT Growth Defrag uses a separate whole-object preparation planner because it immediately rebuilds all objects contiguously in its second phase. exFAT uses a separate whole-object preparation path for the same reason.

### Fragmentation test-data generator

`linux-defragger-testdata DIRECTORY` creates deterministic files, alternating allocation holes and an expanded directory inside `LinuxDefragger-TestData`. The same utility is available from **File → Create fragmented test data…**. It uses normal mounted-filesystem calls, so it can exercise any filesystem Linux can mount read/write. The generated manifest records file sizes and SHA-256 hashes for post-operation verification.

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

The title bar, engine/GUI build label and About dialog read the complete package revision from one version source. Selecting a volume automatically analyses it. Resizing redraws the cached allocation samples in memory and never rescans the device. The File menu provides a new independent window, image opening, fragmented test-data creation, volume refresh and Quit. Separate windows may maintain operations on separate volumes concurrently. The Growth Defrag button is enabled whenever the selected FAT or exFAT backend advertises the growth-layout capability.

## Safe stopping

A Stop request finishes the active journalled transaction and exits between complete transactions or complete Growth Defrag objects. The engine returns status `130` for this deliberate safe stop. The GTK interface treats that status as **Stopped safely**, refreshes the allocation map and does not claim that the interrupted operation completed.

If Growth Defrag is stopped during its preparation compaction, no growth-space layout has begun and no expansion gaps have been applied. If it is stopped during layout, only the complete objects reported in the summary have been repositioned and the requested reserve layout is explicitly marked partial.
