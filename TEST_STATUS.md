# Test status for 1.8.0-23

Validated in this build environment:

- Native Compact ioctl numbers and binary request layouts against the installed Linux UAPI headers for FIEMAP, ext4 move-extent and Btrfs balance/control.
- Btrfs balance request packing for data, metadata and system filters, device ranges and one-chunk-per-class limits.
- Lowest-gap/highest-source planning, partial suffix selection, physical-range merging, capability advertising, GUI dispatch and privileged-helper allowlisting.
- Python syntax checks for the native compact engine, GUI and privileged helper.
- Existing synthetic Btrfs and XFS analyser tests, including corrected Btrfs leaf offsets and multi-level XFS trees.
- Existing focused FAT12/16/32, exFAT, NTFS, EXT, swap, allocation-mapper, Growth Defrag and GUI tests remain present.
- Unified version reporting and package metadata identify revision `1.8.0-23`.

The new ext4, XFS and Btrfs mutation paths require a real block device and a private kernel mount. This container does not have `CAP_SYS_ADMIN`, so it cannot mount loop images and could not execute a destructive physical relocation test. Shannon's removable-media ext4, Btrfs and XFS partitions are therefore the first physical validation targets for these three new compactors. The program must be treated as experimental on them until those tests complete.

Safety boundaries retained in the untested physical paths:

- The GUI and engine require the target to be unmounted before starting.
- ext4/XFS use kernel-journalled mapping exchange calls and retain old high allocations in unlinked donor files until the pass ends.
- If the allocator does not place the donor in the exact requested low hole, no exchange occurs and the pass stops.
- Unsupported inode flags and FIEMAP extent states are skipped. Filesystem metadata is never rewritten directly.
- XFS kernels without `XFS_IOC_EXCHANGE_RANGE` fail before a mapping exchange.
- Btrfs is limited to single-device non-striped layouts and re-reads the physical chunk layout after each balance transaction.
- SIGINT requests a Btrfs balance cancellation or stops ext4/XFS between completed exchange transactions.

## Long-suite status

Focused revision-23 tests pass. The complete historical regression suite is substantially longer and was not used as a substitute for the missing real-device mutation tests.
