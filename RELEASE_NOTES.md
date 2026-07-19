# Linux Defragger 1.8.0 package revision 9

- Reworked native NTFS Compact around the true allocated high-water owner.
- The engine now indexes physical ownership for all readable non-resident NTFS streams before writing.
- It moves only the stream that currently owns the final allocated cluster, then recalculates the boundary.
- An immovable `$MFTMirr`, `$LogFile`, directory index, named/compressed/sparse/encrypted stream, `$ATTRIBUTE_LIST` segment, cross-link, orphan allocation or metadata/bitmap mismatch is reported precisely and stops the operation before unrelated lower files are moved.
- Compact now distinguishes clusters copied from effective boundary reduction and explicitly reports when no useful compaction was achieved.
- GUI progress is based on real high-water reduction toward the theoretical packed boundary rather than the number of candidate MFT records scanned.
- Per-record GUI logging was replaced with periodic summaries; optional detailed move logging is available through `ntfs_engine.py --diagnostic-log PATH`.
- The journalled native NTFS writer, safe Stop path, recovery logic and zero-`ntfs-3g` runtime dependency are unchanged.
