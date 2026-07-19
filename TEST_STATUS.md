# Linux Defragger 1.8.0-28 test status

Passed focused tests:

- Native Compact parser accepts the exact GUI ABI for EXT4/XFS/Btrfs.
- Native Compact module compilation and command-line parsing.
- Btrfs TREE_SEARCH_V2 parser with mixed intermediate key types.
- Btrfs full-key pagination and wanted-type filtering.
- Single-pass Btrfs inode/file-extent and extent/metadata tree scans.
- Existing Btrfs synthetic allocation and fragmentation analysis.
- Existing EXT4/XFS collector, donor-offset, live-map and fixed-point planner tests.
- EXT4 collector accounting uses total `f_bfree` blocks and includes the privileged reserve while retaining its transaction floor.
- GUI metadata rendering blends by density and labels normal filesystem structures separately from bad media.

Physical EXT4 and Btrfs write-path validation remains dependent on the removable-media test volumes.

# Test status for 1.8.0-28

Validated in this build environment:

- Native Compact ioctl numbers and binary request layouts for FIEMAP, ext4 move-extent, XFS range exchange and Btrfs resize.
- Btrfs resize request packing, live chunk-tree parsing and shrink-target planning.
- Lowest-gap/highest-source planning, partial suffix selection, physical-range merging, capability advertising, GUI dispatch and privileged-helper allowlisting.
- Python syntax checks for the native compact engine, GUI and privileged helper.
- Existing synthetic Btrfs and XFS analyser tests, including corrected Btrfs leaf offsets and multi-level XFS trees.
- Existing focused FAT12/16/32, exFAT, NTFS, EXT, swap, allocation-mapper, Growth Defrag and GUI tests remain present.
- Unified version reporting and package metadata identify revision `1.8.0-28`.


Revision-24 regression coverage additionally validates:

- Collector extents retain their owning file descriptor and logical offset as well as their physical address.
- The direct donor slice is rechecked through FIEMAP before every move.
- Staged writes honour nonzero donor-file offsets.
- ext4 and XFS ioctl requests carry the correct collector donor offset.
- The extent compactor no longer contains the hole-punch/second-`fallocate` donor path that caused the physical EXT4 `ENOSPC` failure.


Revision-25 focused coverage additionally validates:

- Structured `@@LIVE_RANGE` messages contain the exact source, destination, length, pass number and cumulative moved-byte count.
- The GTK stream handler applies those ranges to the cached allocation cells and redraws without launching another analysis.
- The extent compactor repeats collector passes until a no-progress pass establishes a fixed point, with a 32-pass safety ceiling.
- EXT reserved system inodes are excluded from ordinary file counts and their allocations are classified as reserved.
- Known EXT descriptor, bitmap and inode-table ranges are classified as reserved on the map.

The new ext4, XFS and Btrfs mutation paths require a real block device and a private kernel mount. This container does not have `CAP_SYS_ADMIN`, so it cannot mount loop images and could not execute a destructive physical relocation test. Shannon's removable-media ext4, Btrfs and XFS partitions are therefore the first physical validation targets for these three new compactors. The program must be treated as experimental on them until those tests complete.

Safety boundaries retained in the untested physical paths:

- The GUI and engine require the target to be unmounted before starting.
- ext4/XFS use kernel-journalled mapping exchange calls and retain old high allocations in unlinked donor files until the pass ends.
- If the allocator does not place the donor in the exact requested low hole, no exchange occurs and the pass stops.
- Unsupported inode flags and FIEMAP extent states are skipped. Filesystem metadata is never rewritten directly.
- XFS kernels without `XFS_IOC_EXCHANGE_RANGE` fail before a mapping exchange.
- Btrfs is limited to single-device non-striped layouts and re-reads the physical chunk layout after each shrink-and-restore cycle.
- SIGINT stops Btrfs before the next resize cycle or stops ext4/XFS between completed exchange transactions.

## Long-suite status

Focused revision-25 tests pass. The complete historical regression suite is substantially longer and was not used as a substitute for the missing real-device mutation tests.
