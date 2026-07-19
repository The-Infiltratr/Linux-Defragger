# Linux Defragger 1.6.0 test status

The Amiga backend was tested on generated 880 KiB ADF images for DOS\0 through DOS\7.

- All eight variants opened and produced exact allocation maps.
- Deliberately fragmented files were detected on every variant.
- Full defragmentation reduced fragmented-file counts to zero.
- SHA-256 hashes of every file matched before and after relocation.
- Nested directory trees remained readable after compaction on FFS, FFS directory-cache and FFS long-name images.
- Pre-switch interruption recovery rolled back destination blocks.
- Post-switch interruption recovery retained the new object and freed the old blocks.

Physical Amiga media was not available in the build environment. The first physical-media run should use replaceable media.
