# Linux Defragger 1.8.0-35

Linux Defragger provides graphical allocation maps, fragmentation analysis, offline free-space compaction, file defragmentation, FAT/exFAT growth-space layouts and journalled recovery for supported filesystems.

The user-visible operations are deliberately separate:

- **Analyse** reads filesystem metadata and reports allocation, free space and fragmentation. It is read-only and may run against a mounted volume as a live snapshot.
- **Compact** fills internal free-space gaps so free space moves toward the physical end of the volume. It is not a file-defragmentation command.
- **Defragment** rebuilds fragmented files into contiguous allocation runs. It does not attempt to pack every free-space gap.
- **Growth Defrag** is a FAT12/16/32 and exFAT layout operation. It verifies the existing layout first, then rebuilds allocated objects in physical order and deliberately leaves free expansion room after each regular file only when work is required.

## Architecture

Revision 35 standardises the program around one orchestration engine and a validated filesystem-plugin ABI. The GTK interface consumes plugin manifests; `operation_engine.py` dispatches every mutation; `gui/core` contains common policy, path, mount-state and journal modules; and each module in `gui/backends` declares its analysis implementation and exact mutation operations. Filesystem-specific algorithms remain in independent workers so they can be tested without the GUI.

The root helper no longer contains separate FAT, exFAT, NTFS, Apple, Amiga, EXT4, Btrfs or XFS dispatch branches. It permits the read-only mapper and central operation engine, while the plugin registry selects only fixed known workers. Detailed contracts are documented in `docs/BACKEND_ABI.md` and `docs/DESIGN.md`.

Shared raw-device reads now retry short positional reads, and shared range aggregation walks sorted ranges and display cells linearly rather than rescanning every range for every cell.

## EXT4 Compact boundary

EXT4 Compact is an iterative offline filesystem-wide operation. It starts with `e2fsck -D`, shrinks the filesystem to its current minimum, temporarily restores the original filesystem geometry only when a regular-file low-hole packing round is needed, checks it again, and repeats. A final minimum-size shrink is always performed after the last packing round.

The final compacted filesystem remains at that verified minimum size. The partition table and partition size are not changed; the remaining physical partition tail is outside ext4 and is displayed as **Packed tail outside filesystem**. No file, directory, journal, inode table, bitmap or other ext4 allocation can exist in that outside tail.

During an intermediate packing round Linux Defragger privately mounts the checked filesystem and uses `EXT4_IOC_MOVE_EXT` to exchange higher regular-file extents into lower holes. It then unmounts, runs another forced filesystem check and shrinks again so directory and relocatable metadata allocations are consolidated around the denser file layout. The final verification is read-only and cannot create fresh allocations.


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
- XFS: genuine native physical allocation maps, file/directory fragmentation analysis and native whole-file Compact through the long-established atomic `XFS_IOC_SWAPEXT` interface. Linux 6.10 is not required. Realtime-file data on a separate realtime device is reported but is not moved by Compact.
- UFS, ZFS, APFS, Minix and swap: read-only allocation and fragmentation analysis where supported by the backend.

### Native ext4, XFS and Btrfs Compact semantics

EXT4 Compact combines two kernel-supported mechanisms. Offline `resize2fs -M` rounds relocate the complete filesystem below the smallest currently valid boundary. Between those rounds, a private kernel mount reserves the free map in an unlinked collector and uses `EXT4_IOC_MOVE_EXT` to exchange high regular-file extents directly into verified low collector ranges. The final `resize2fs -M` result is deliberately not expanded again: a full-size ext4 filesystem requires block-group metadata throughout its geometry, so leaving the filesystem at minimum size is the only non-reformatting method that guarantees an allocation-free physical tail. The collector counts all `f_bfree` blocks, including the privileged reserve, while retaining a 64 MiB transaction-safety floor.

XFS Compact reserves the current free map in an unlinked collector, releases one exact low range at a time, creates a compatible temporary file in that range, copies one complete source file while preserving its logical holes, and atomically exchanges the complete XFS data forks with `XFS_IOC_SWAPEXT`. The source inode keeps its identity and metadata while receiving the lower donor allocation. A move is accepted only when the donor lies entirely inside the selected lower range and has no more physical extents than the source, so Compact cannot increase the moved file's fragment count. Collector passes repeat to a fixed point and report live physical updates. Immutable, append-only, DAX, realtime, shared, unwritten, encoded and otherwise unsafe files are left unchanged.

Btrfs Compact cannot safely exchange arbitrary file extents because they are copy-on-write and back-referenced. It therefore runs a native `BTRFS_IOC_BALANCE_V2` data/metadata balance to consolidate partially used block groups and release chunks, with live progress and cancellation through the balance-control ioctl. It then performs progressively tighter `BTRFS_IOC_RESIZE` shrink-and-restore cycles so the surviving chunks are forced toward the physical beginning. Up to three balance-and-shrink rounds are attempted before declaring a fixed point. File defragmentation is not invoked.

The Btrfs analyser uses the kernel `TREE_SEARCH_V2` interface. Revision 31 increases each request from 4,096 to 131,072 items, uses a 16 MiB result buffer, and avoids copying payloads for unwanted intermediate key types. This substantially reduces analysis time on extent trees containing many records while preserving the exact single-device physical map.


### FAT and exFAT Compact semantics

FAT and exFAT Compact are intentionally pure compactors. They fill the lowest free clusters with movable allocation copied from the physical tail. They do not prefer complete files and do not attempt to reduce fragmentation; a file may become more fragmented as the price of removing internal gaps. FAT Growth Defrag uses a separate whole-object preparation planner because it immediately rebuilds all objects contiguously in its second phase. exFAT uses a separate whole-object preparation path for the same reason.

### Fragmentation test-data generator

`linux-defragger-testdata DIRECTORY` creates deterministic files, alternating allocation holes and an expanded directory inside `LinuxDefragger-TestData`. The same utility is available from **File → Create fragmented test data…**. It uses normal mounted-filesystem calls, so it can exercise any filesystem Linux can mount read/write. The generated manifest records file sizes and SHA-256 hashes for post-operation verification.

## Native NTFS operations

NTFS support is implemented inside Linux Defragger and has no NTFS-3G runtime dependency. The engine reads and writes the NTFS boot geometry, MFT records, mapping pairs, `$Bitmap`, `$Volume` and `$MFTMirr` directly.

### NTFS Compact

NTFS Compact fills low internal free gaps using complete supported ordinary-file and directory-index streams. A stream is moved only when its entire allocation fits one lower contiguous gap and all of its current physical extents are above that gap. Each move therefore preserves a contiguous stream or consolidates a fragmented stream into one extent; Compact never splits a file or increases its fragment count merely to consume a small hole.

Each completed journalled whole-stream transaction emits exact physical source and destination ranges. The GTK allocation map updates immediately during NTFS Compact instead of waiting for the final analysis. Gaps too small for any complete higher stream remain free rather than being filled by fragmenting a file.


### NTFS Defragment

NTFS Defragment finds supported streams that have more than one physical extent and copies each complete stream into the lowest suitable contiguous free run. If only a higher run is large enough, it is used as temporary staging. Subsequent settling passes reuse the released source space and move the still-contiguous stream downward again wherever a lower complete run fits. No Defragment transaction creates a new fragment.

### NTFS recovery and current limits

Every NTFS move uses an external journal. Data is copied first, destination clusters are reserved, the MFT record is switched, old clusters are released, and the original clean volume flags are restored. Recover inspects the actual on-disk MFT record and completes the transaction forward or restores the original record and bitmap state.

The current native writer deliberately leaves NTFS system files, unsupported directory metadata, named data streams, compressed/sparse/encrypted streams, `$ATTRIBUTE_LIST` extension streams and malformed MFT records unchanged. Supported directory `$INDEX_ALLOCATION` streams participate in Compact.

## Interface

The title bar, engine/GUI build label and About dialog read the complete package revision from one version source. Selecting a volume automatically analyses it. Resizing redraws the cached allocation samples in memory and never rescans the device. The File menu provides a new independent window, image opening, fragmented test-data creation, volume refresh and Quit. Separate windows may maintain operations on separate volumes concurrently. The Growth Defrag button is enabled whenever the selected FAT or exFAT backend advertises the growth-layout capability.

## Safe stopping

A Stop request finishes the active journalled transaction and exits between complete transactions or complete Growth Defrag objects. The engine returns status `130` for this deliberate safe stop. The GTK interface treats that status as **Stopped safely**, refreshes the allocation map and does not claim that the interrupted operation completed.

If Growth Defrag is stopped during its preparation compaction, no growth-space layout has begun and no expansion gaps have been applied. If it is stopped during layout, only the complete objects reported in the summary have been repositioned and the requested reserve layout is explicitly marked partial.
