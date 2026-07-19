# Linux Defragger 1.8.0-31

Linux Defragger provides graphical allocation maps, fragmentation analysis, offline free-space compaction, file defragmentation, FAT/exFAT growth-space layouts and journalled recovery for supported filesystems.

The user-visible operations are deliberately separate:

- **Analyse** reads filesystem metadata and reports allocation, free space and fragmentation. It is read-only and may run against a mounted volume as a live snapshot.
- **Compact** fills internal free-space gaps so free space moves toward the physical end of the volume. It is not a file-defragmentation command.
- **Defragment** rebuilds fragmented files into contiguous allocation runs. It does not attempt to pack every free-space gap.
- **Growth Defrag** is a FAT12/16/32 and exFAT layout operation. It verifies the existing layout first, then rebuilds allocated objects in physical order and deliberately leaves free expansion room after each regular file only when work is required.

## EXT4 Compact boundary

EXT4 Compact is an iterative filesystem-wide operation. It first runs an offline `e2fsck -D` pass to optimise directory indexes, shrinks the filesystem to its minimum valid size, and restores the exact original filesystem size. That shrink forces ordinary files, directory blocks, the journal and relocatable metadata out of the physical tail.

A minimum-size shrink alone can still leave lower holes inside the temporary minimum image. After each restore, Linux Defragger privately mounts the volume and uses `EXT4_IOC_MOVE_EXT` to exchange higher regular-file extents into those lower holes. It then shrinks and restores again so directory and metadata allocations can be relocated around the denser file layout. The rounds stop when a complete regular-file pass moves nothing, with a fixed safety limit and an additional final shrink after the last productive pass.

The partition table is never changed. The final verification is read-only so it cannot allocate fresh directory or metadata blocks in the restored tail. Any allocations beyond the reported packed boundary are block-group structures required by the restored full-size EXT4 geometry, not ordinary file or directory data. The allocation map labels those structures **Filesystem metadata/reserved** and blends their colour by cell density.


## FAT and exFAT Growth Defrag

The GTK **Growth Defrag** button requests a 10 percent reserve after every non-empty regular file on FAT and exFAT. The native FAT engine also accepts `--growth-percent 1..25` for direct command-line use.

The reserve is measured in complete FAT clusters. Each file receives `ceil(file_clusters × percentage / 100)` free clusters, so a very small file receives a one-cluster minimum at the default 10 percent. Directories remain contiguous but do not receive a growth gap.

Growth Defrag works in two durable phases:

1. Compact the FAT allocation so the terminal free area can be used as an overlap-safe workspace.
2. Preserve the current physical object order, rebuild each file or directory as one contiguous chain, and insert the requested free gap after each regular file.

Objects are placed from the end of the plan backwards. When a destination overlaps the object's current allocation, the complete object is first moved into the reusable terminal workspace. If clusters from another fragmented chain still occupy the target, they are evacuated into the staged object's released source clusters outside that target; the staged object is then placed at its final location. This resolves interleaved-chain dependencies without increasing the required workspace beyond the largest allocated object. Every move uses the normal external mapped-cluster journal, so Recover can finish an interrupted transaction.

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
- EXT2/3/4: read-only allocation maps and file/directory fragmentation analysis. Actual ext4 extent-format volumes also support native Compact; ext2, ext3 and ext4 bigalloc remain analysis-only.
- Btrfs: genuine native physical allocation maps, file-fragmentation analysis and balance-and-shrink Compact for single-device filesystems using non-striped local profiles. Multi-device and striped RAID profiles remain conservatively unsupported.
- XFS: genuine native physical allocation maps, file/directory fragmentation analysis and range-level Compact on kernels that provide `XFS_IOC_EXCHANGE_RANGE` (Linux 6.10 or newer). Realtime-file data on a separate realtime device is reported but is not moved by Compact.
- UFS, ZFS, APFS, Minix and swap: read-only allocation and fragmentation analysis where supported by the backend.

### Native ext4, XFS and Btrfs Compact semantics

EXT4 Compact combines two kernel-supported mechanisms. Offline `resize2fs -M` rounds relocate the complete filesystem below the smallest currently valid boundary. Between those rounds, a private kernel mount reserves the free map in an unlinked collector and uses `EXT4_IOC_MOVE_EXT` to exchange high regular-file extents directly into verified low collector ranges. The collector counts all `f_bfree` blocks, including the privileged reserve, while retaining a 64 MiB transaction-safety floor. Completed exchanges are journalled by EXT4 and are sent to the GUI as live physical source/destination updates.

XFS Compact retains the range-exchange collector design. It requires a kernel providing `XFS_IOC_EXCHANGE_RANGE`, verifies every donor mapping immediately before exchange, repeats collector passes to a fixed point, and reports live physical updates. Immutable, append-only, DAX, realtime, shared, unwritten, encoded and otherwise unsafe extents are left unchanged.

Btrfs Compact cannot safely exchange arbitrary file extents because they are copy-on-write and back-referenced. It therefore runs a native `BTRFS_IOC_BALANCE_V2` data/metadata balance to consolidate partially used block groups and release chunks, with live progress and cancellation through the balance-control ioctl. It then performs progressively tighter `BTRFS_IOC_RESIZE` shrink-and-restore cycles so the surviving chunks are forced toward the physical beginning. Up to three balance-and-shrink rounds are attempted before declaring a fixed point. File defragmentation is not invoked.

The Btrfs analyser uses the kernel `TREE_SEARCH_V2` interface. Revision 31 increases each request from 4,096 to 131,072 items, uses a 16 MiB result buffer, and avoids copying payloads for unwanted intermediate key types. This substantially reduces analysis time on extent trees containing many records while preserving the exact single-device physical map.


### FAT and exFAT Compact semantics

FAT and exFAT Compact are intentionally pure compactors. They fill the lowest free clusters with movable allocation copied from the physical tail. They do not prefer complete files and do not attempt to reduce fragmentation; a file may become more fragmented as the price of removing internal gaps. FAT Growth Defrag uses a separate whole-object preparation planner because it immediately rebuilds all objects contiguously in its second phase. exFAT uses a separate whole-object preparation path for the same reason.

### Fragmentation test-data generator

`linux-defragger-testdata DIRECTORY` creates deterministic files, alternating allocation holes and an expanded directory inside `LinuxDefragger-TestData`. The same utility is available from **File → Create fragmented test data…**. It uses normal mounted-filesystem calls, so it can exercise any filesystem Linux can mount read/write. The generated manifest records file sizes and SHA-256 hashes for post-operation verification.

## Native NTFS operations

NTFS support is implemented inside Linux Defragger and has no NTFS-3G runtime dependency. The engine reads and writes the NTFS boot geometry, MFT records, mapping pairs, `$Bitmap`, `$Volume` and `$MFTMirr` directly.

### NTFS Compact

NTFS Compact fills the lowest internal free gaps from supported higher ordinary-file and directory-index streams. A source extent may be split across smaller lower gaps, and a one-cluster gap can be filled from a one-cluster slice. Increasing the file's fragment count is permitted because physical free-space packing is the purpose of Compact; Defragment remains the separate operation that rebuilds files contiguously.

Each completed journalled slice transaction emits exact physical source and destination ranges. The GTK allocation map updates immediately during NTFS Compact instead of waiting for the final analysis. The fragmentation overlay is recalculated after completion because a packing move may deliberately split a stream.


### NTFS Defragment

NTFS Defragment finds supported ordinary files that have more than one physical extent. Each file is copied in logical order into the highest suitable contiguous free run anywhere on the volume. Freed source holes are not reused during the same defragmentation pass, keeping Defragment separate from Compact.

### NTFS recovery and current limits

Every NTFS move uses an external journal. Data is copied first, destination clusters are reserved, the MFT record is switched, old clusters are released, and the original clean volume flags are restored. Recover inspects the actual on-disk MFT record and completes the transaction forward or restores the original record and bitmap state.

The current native writer deliberately leaves NTFS system files, unsupported directory metadata, named data streams, compressed/sparse/encrypted streams, `$ATTRIBUTE_LIST` extension streams and malformed MFT records unchanged. Supported directory `$INDEX_ALLOCATION` streams participate in Compact.

## Interface

The title bar, engine/GUI build label and About dialog read the complete package revision from one version source. Selecting a volume automatically analyses it. Resizing redraws the cached allocation samples in memory and never rescans the device. The File menu provides a new independent window, image opening, fragmented test-data creation, volume refresh and Quit. Separate windows may maintain operations on separate volumes concurrently. The Growth Defrag button is enabled whenever the selected FAT or exFAT backend advertises the growth-layout capability.

## Safe stopping

A Stop request finishes the active journalled transaction and exits between complete transactions or complete Growth Defrag objects. The engine returns status `130` for this deliberate safe stop. The GTK interface treats that status as **Stopped safely**, refreshes the allocation map and does not claim that the interrupted operation completed.

If Growth Defrag is stopped during its preparation compaction, no growth-space layout has begun and no expansion gaps have been applied. If it is stopped during layout, only the complete objects reported in the summary have been repositioned and the requested reserve layout is explicitly marked partial.
