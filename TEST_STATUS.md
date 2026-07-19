# Linux Defragger 1.8.0-31 test status

Passed in this build environment:

- Python syntax and import checks for every GUI/backend engine.
- Native Compact ABI parser and ioctl-number/layout checks.
- Btrfs balance request layout against the installed Linux `btrfs.h` structure offsets and ioctl values.
- Btrfs balance worker progress polling, fixed-point shrink target planning, kernel tree-search pagination and intermediate-key filtering.
- EXT4 iterative shrink/restore and embedded regular-file packing orchestration, including directory optimisation and read-only final verification.
- NTFS split-run live allocation events, disabled-live-map behaviour, partial-extent compaction, payload integrity and independent `ntfsresize --check` validation where NTFS utilities are available.
- GTK live range handling, operation-aware live status text and Gdk version declaration.
- Existing focused FAT12/16/32, exFAT, NTFS, EXT, Btrfs, XFS, swap, allocation mapper, Growth Defrag and GUI tests.

Physical EXT4 and Btrfs mutation still requires a real block-device partition and `CAP_SYS_ADMIN`, which this container does not provide. Shannon's removable test partitions remain the physical validation environment. The code restores the original filesystem size in cleanup paths and stops between complete kernel-journalled transactions.

## Long-suite status

The complete historical `tests/run_tests.sh` suite ran for ten minutes and reached the journal-recovery tests without reporting a failure, but it did not finish before the execution limit. The focused revision-31 tests and all directly affected backend tests completed successfully.
