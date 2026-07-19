# Test status for 1.8.0-14

- Python syntax compilation passed for the GUI, privileged helper, backends and native NTFS engine.
- Synthetic native NTFS Defragment tests rebuild a two-extent file as one contiguous extent and preserve the exact SHA-256 payload.
- A new destructive test fills the physical tail, leaves a suitable internal free run, and confirms Defragment still rebuilds the file into one extent.
- The regression test confirms the old trailing-only planner would have had no destination while the corrected whole-volume planner succeeds.
- Synthetic native NTFS Compact tests confirm complete extents may move downward while the file's physical fragment count remains unchanged.
- Native NTFS forward recovery, rollback recovery, bitmap range updates and preserved `0x0080` volume-flag tests passed.
- Existing NTFS allocation/MFT fragmentation analysis tests passed.
- Real formatted NTFS compact and defragment tests remain in the test suite and automatically run when `mkntfs`, `ntfscp`, `ntfscat` and `ntfsresize` are available. Those independent utilities were unavailable in the current build container, so those optional tests were skipped rather than claimed as executed.
- Focused EXT, swap, Apple-backend, allocation-mapper and mounted-analysis policy tests passed after the change.
- The C engines compiled cleanly with the normal warning set, and the packaged FAT engine reports `1.8.0-14`.
- Static GUI checks confirmed the title, File menu, About menu, Defragment control and operation-specific progress wording.
- Existing FAT, exFAT and Amiga destructive regression suites remain unchanged and present.
