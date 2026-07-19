# Linux Defragger 1.8.0-23

- Adds native Compact capability to actual ext4, supported single-device Btrfs and XFS volumes. Existing FAT, exFAT, NTFS, Amiga and Apple compactors are unchanged.
- Keeps Compact separate from Defragment: ext4 and XFS paste high regular-file extents into low physical holes even when that increases fragmentation; Btrfs consolidates physical chunks through filtered balance transactions and never invokes file defragmentation.
- Uses private mounts and filesystem kernel transaction engines instead of external `e4defrag`, `xfs_fsr`, `btrfs` or other command-line utilities.
- Uses `EXT4_IOC_MOVE_EXT` for ext4 and the atomic `XFS_IOC_EXCHANGE_RANGE` interface for XFS. XFS Compact reports a clear unsupported-kernel result on systems older than Linux 6.10.
- Uses unlinked collector and donor files for ext4/XFS. They reserve the current free map, expose one exact low hole at a time and retain exchanged high blocks until the pass ends, after which the temporary files disappear and the tail becomes free.
- Skips immutable, append-only, DAX, XFS realtime, shared, unwritten, encoded and other unsafe file extents. Filesystem metadata remains an immovable barrier.
- Adds single-device Btrfs chunk compaction with device-range and transaction-limit filters, physical-layout rechecks after every balance and cancellation through the native balance-control ioctl.
- Enables the Compact button for ext4, Btrfs and XFS while leaving Defragment disabled for those filesystems. ext2, ext3 and ext4 bigalloc remain analysis-only.
- Adds focused structural tests for ioctl values, Btrfs request packing, source selection, partial tail moves, capability wiring and privileged-helper dispatch.

## Revision 22

- Corrects the native Btrfs leaf-item parser. On disk, each leaf item data offset is relative to the end of the 101-byte tree header; revision 21 incorrectly treated it as an absolute block offset.
- Updates the Btrfs regression image to use the genuine on-disk offset encoding, so the former parser mistake can no longer pass its own synthetic test.
- Keeps Btrfs and XFS write operations disabled in that revision.

## Revision 21

- Replaces the Btrfs aggregate-summary placeholder with a genuine native read-only analyser for supported single-device filesystems.
- Walks the Btrfs system chunk array, chunk tree, root tree, extent tree and live filesystem trees directly, without invoking `btrfs-progs`.
- Produces an exact physical allocation map for local SINGLE, DUP and same-device mirrored profiles, including metadata, data extents and superblock mirrors.
- Counts Btrfs regular files and directories and identifies fragmented regular files from their physical file-extent records. Directory fragmentation remains not applicable because Btrfs directories share filesystem-tree blocks rather than owning private allocation chains.
- Rejects multi-device or striped Btrfs profiles rather than presenting an invented physical layout.
- Replaces the XFS summary placeholder with a genuine native read-only allocation-group and inode analyser.
- Walks each XFS allocation group's block-number free-space B+tree, inode B+tree and allocated inode data fork, including direct extents and external bmap B+trees.
- Fixes the old XFS free-space accounting bug that could incorrectly report an almost-full filesystem.
- Produces exact XFS free/used maps and file/directory fragmentation overlays on the data device.
- Keeps all Btrfs and XFS mutation capabilities disabled. Analyse and Map remain the only advertised operations.
- Adds focused synthetic multi-level-tree regression images for both filesystems and updates the backend documentation and package revision to `1.8.0-21`.
