# Test status for 1.8.0-21

Validated in controlled filesystem images:

- Btrfs single-device analysis using a synthetic filesystem with a system chunk array, mirrored metadata chunks, a multi-level filesystem tree, extent-tree allocations, two regular files and one directory.
- Btrfs exact physical map accounting, free-space complement, metadata/data allocation, superblock reservation and detection of a two-extent fragmented file.
- Btrfs conservative rejection paths for unsupported or contradictory physical mappings remain active.
- XFS v5 analysis using a synthetic filesystem with multi-level allocation-group free-space and inode B+trees, sparse inode records, direct inode extents and an external bmap B+tree.
- XFS exact free/used data-device mapping, regular-file and directory counts, fragmented-file detection and physical fragmentation overlays.
- Existing FAT12/16/32, exFAT, NTFS, EXT, swap, allocation-mapper and GUI focused tests remain present.
- Unified version reporting and package metadata now identify revision `1.8.0-21`.

The Btrfs and XFS analysers are read-only and do not depend on `btrfs-progs` or `xfsprogs` at runtime. The synthetic tests exercise the on-disk parsers and map builders without mutating the images.

Physical Btrfs and XFS removable-media partitions have not yet been validated in this build environment. Shannon's multi-filesystem SD-card test is the first intended real-device validation, and any mismatch should be treated as a parser bug until investigated.
