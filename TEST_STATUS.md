# Test status for 1.8.0-13

- Python syntax compilation passed for the GUI, privileged helper, backends and native NTFS engine.
- NTFS backend capability tests confirm Analyse, Map, Compact, Defragment and Recover are advertised independently.
- Synthetic native NTFS Compact tests confirm complete extents may move downward while the file's physical fragment count remains unchanged.
- Synthetic split-hole tests confirm Compact refuses to split a contiguous extent across several smaller holes.
- Synthetic native NTFS Defragment tests rebuild a two-extent file as one contiguous extent near the physical end and preserve the exact SHA-256 payload.
- Native NTFS forward recovery, rollback recovery, bitmap range updates and preserved `0x0080` volume-flag tests passed.
- Existing NTFS allocation/MFT fragmentation analysis tests passed.
- Real formatted NTFS compact and defragment tests remain in the test suite and automatically run when `mkntfs`, `ntfscp`, `ntfscat` and `ntfsresize` are available. Those independent utilities were unavailable in the current build container, so those optional tests were skipped here rather than claimed as executed.
- Focused EXT, swap, Apple-backend, allocation-mapper and mounted-analysis policy tests passed after the change.
- The C engines compiled cleanly with the normal warning set, and the packaged FAT engine reports `1.8.0-13`.
- Static GUI checks confirmed the title, File menu, About menu, Defragment control and operation-specific progress wording. A live GTK window test was not available in the build container because its Python GTK bindings were absent.
- Existing FAT, exFAT and Amiga destructive regression suites remain unchanged and present.
