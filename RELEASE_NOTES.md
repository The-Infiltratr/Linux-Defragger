# Linux Defragger 1.8.0-22

- Corrects the native Btrfs leaf-item parser. On disk, each leaf item data offset is relative to the end of the 101-byte tree header; revision 21 incorrectly treated it as an absolute block offset.
- Updates the Btrfs regression image to use the genuine on-disk offset encoding, so the former parser mistake can no longer pass its own synthetic test.
- Keeps Btrfs strictly read-only and leaves XFS unchanged.

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
