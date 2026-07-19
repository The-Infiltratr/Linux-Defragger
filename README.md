# Linux Defragger 1.8.0-29

Linux Defragger provides graphical allocation maps, fragmentation analysis, offline free-space compaction, file defragmentation, FAT/exFAT growth-space layouts and journalled recovery for supported filesystems.

The user-visible operations are deliberately separate:

- **Analyse** reads filesystem metadata and reports allocation, free space and fragmentation. It is read-only and may run against a mounted volume as a live snapshot.
- **Compact** fills internal free-space gaps so free space moves toward the physical end of the volume. It is not a file-defragmentation command.
- **Defragment** rebuilds fragmented files into contiguous allocation runs. It does not attempt to pack every free-space gap.
- **Growth Defrag** is a FAT12/16/32 and exFAT layout operation. It verifies the existing layout first, then rebuilds allocated objects in physical order and deliberately leaves free expansion room after each regular file only when work is required.

## EXT4 Compact boundary

EXT4 Compact packs movable regular-file extents into lower free ranges. The privileged collector counts `f_bfree`, so it includes EXT4's root-reserved free-block pool instead of leaving those blocks as unreachable white gaps. It still leaves a 64 MiB safety floor for journal and extent-tree metadata work, and its fallocate retry path backs off if the filesystem cannot provide every reported block.

An unmounted EXT4 filesystem still has physically allocated block-group descriptors, bitmaps, inode tables, journal blocks and directory data. Those structures are part of the filesystem format rather than activity caused by mounting, and the kernel regular-file extent-exchange interface cannot relocate all of them. The allocation map identifies them as **Filesystem metadata/reserved** or directory allocation. Metadata colour is blended according to the proportion of each sampled cell, so one metadata block no longer turns an entire multi-block map pixel black.


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
- Btrfs: genuine native physical allocation maps, file-fragmentation analysis and chunk-level Compact for single-device filesystems using non-striped local profiles. Multi-device and striped RAID profiles remain conservatively unsupported.
- XFS: genuine native physical allocation maps, file/directory fragmentation analysis and range-level Compact on kernels that provide `XFS_IOC_EXCHANGE_RANGE` (Linux 6.10 or newer). Realtime-file data on a separate realtime device is reported but is not moved by Compact.
- UFS, ZFS, APFS, Minix and swap: read-only allocation and fragmentation analysis where supported by the backend.

### Native ext4, XFS and Btrfs Compact semantics

The ext4 and XFS compactors use the mounted kernel filesystem driver rather than editing live metadata structures themselves. The GUI still requires the volume to be unmounted; the privileged engine mounts it privately and reserves accessible free extents in unlinked collector files. Each lowest collector extent is then used directly as the donor for an exchange with a high regular-file extent. The operation does not punch that range free and does not request a second allocation after the collector has reserved the free-space map. This avoids a self-inflicted `ENOSPC` failure and keeps the exact physical destination under verification before every exchange. The operation is deliberately allowed to split files and increase fragmentation. Immutable, append-only, DAX, realtime, shared, unwritten, encoded and otherwise unsupported extents are left in place. Filesystem metadata is not moved.

Each ext4 move uses `EXT4_IOC_MOVE_EXT`; each supported XFS move uses the atomic `XFS_IOC_EXCHANGE_RANGE` interface. The collector files are unlinked and held only by open descriptors. After an exchange they own the old high blocks; closing them at the end releases those blocks together at the physical tail. The engine then creates a fresh collector and repeats automatically until a complete pass moves no more regular-file data, so newly exposed opportunities do not require another click. A safe Stop exits between completed kernel-journalled exchanges or between complete collector passes.

During ext4/XFS Compact, each completed exchange sends a physical source/destination range to the GUI. The cached allocation samples are updated and redrawn immediately without rereading the raw device. This live view shows used/free movement; the exact fragmentation overlay is deliberately rebuilt by the final read-only analysis because a compact move may split a file.

EXT filesystems contain fixed structures distributed through block groups. The analyser marks known superblock/descriptor areas, block and inode bitmaps, inode tables, and low-numbered system-inode allocations as **Filesystem metadata/reserved**, while directory extents remain purple. The renderer blends these categories by cell density rather than painting a whole cell from a single metadata block. These allocations can remain as islands inside the free tail because the online regular-file extent-exchange interface cannot relocate them. Compact nevertheless includes the privileged free-block reserve and packs all movable regular-file data as low as those fixed structures permit.

Btrfs Compact is necessarily different because allocated extents are copy-on-write and back-referenced. It uses a native online shrink-and-restore transaction to force high physical chunks below a temporary boundary, then restores the original filesystem size. It never invokes the file-defragmentation ioctl. The engine measures the physical chunk layout after each resize cycle and stops when the chunk boundary no longer improves.

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
