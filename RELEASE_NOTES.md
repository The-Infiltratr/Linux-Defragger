# Linux Defragger 1.8.0 package revision 7

- Replaced the rejected NTFS-3G/`ntfsresize` shortcut with Linux Defragger's own native NTFS compaction engine.
- Removed the `ntfs-3g` runtime package dependency.
- Added direct NTFS mapping-pairs encoding, MFT update-sequence handling, `$Bitmap` allocation updates, `$Volume`/`$MFTMirr` dirty-state handling and external interrupted-move recovery.
- NTFS Compact now relocates conservative ordinary file-data streams toward lower free clusters without resizing the filesystem or changing the partition table.
- NTFS system metadata, directories, compressed, sparse, encrypted and attribute-list streams remain intentionally immovable in this first native stage.
