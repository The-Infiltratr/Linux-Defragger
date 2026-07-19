# Linux Defragger 1.8.0-25

- Adds real-time EXT4 and XFS Compact allocation-map updates. Every completed kernel mapping exchange reports the low destination and released high source range to the GUI, which redraws the cached map without rescanning the device.
- Keeps the live display honest: physical used/free movement is updated during Compact, while exact fragmentation colours are recalculated by the normal final analysis.
- Makes EXT4/XFS Compact automatically repeat complete collector passes until a fresh pass moves no more regular-file data. A second click is no longer required merely because releasing the first pass collector exposed additional compaction opportunities.
- Uses monotonic multi-pass progress reporting and retains safe stopping between completed kernel-journalled transactions and between collector passes.
- Corrects EXT allocation reporting so reserved low-numbered system inodes such as the journal and resize inode are not counted as ordinary files or fragmented user data.
- Marks known EXT superblock/descriptor, block-bitmap, inode-bitmap, inode-table and reserved-system-inode allocations as **Bad/reserved** on the map. This distinguishes fixed filesystem structures from movable blue file data.
- Reports the online compaction boundary explicitly: remaining islands in the free tail can be directories, journals, allocation metadata or other mappings that the regular-file extent-exchange interface cannot relocate.

# Revision 24

- Fixes the first physical EXT4 Compact failure, where the space collector reserved the free-space map and then attempted to allocate a second donor file, causing `ENOSPC` despite abundant free space.
- Uses the collector's already allocated low extents directly as exchange donors for both ext4 and XFS. No hole punch or second `fallocate` is performed after the collector pass.
- Preserves the collector file descriptor, logical offset and physical offset for every reserved range and re-verifies that mapping immediately before each exchange.
- Copies staged file data into the exact collector logical range, passes the nonzero donor offset to `EXT4_IOC_MOVE_EXT` or `XFS_IOC_EXCHANGE_RANGE`, and leaves the old high extent owned by the collector until final cleanup.
- Raises the untouched free-space floor from 4 MiB to 64 MiB and uses `f_bavail` accounting, leaving room for filesystem metadata and kernel transactions.
- Adds focused regression tests for collector mapping preservation, nonzero donor offsets, direct-donor request packing and the absence of the failed second-allocation path.

# Revision 23

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
