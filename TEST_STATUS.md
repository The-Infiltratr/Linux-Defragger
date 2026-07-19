# Linux Defragger 1.8.0-32 test status

Revision 32 adds regression coverage for an ext4 filesystem smaller than its containing device, physical packed-tail rendering, indexed NTFS live-range application, redraw throttling and active-request safe-stop dispatch.

Passed in this build environment:

- Python syntax and import checks for every GUI/backend engine.
- Native Compact ABI parser and ioctl-number/layout checks.
- Btrfs balance request layout against the installed Linux `btrfs.h` structure offsets and ioctl values.
- Btrfs balance worker progress polling, fixed-point shrink target planning, kernel tree-search pagination and intermediate-key filtering.
- EXT4 iterative shrink/restore and embedded regular-file packing orchestration, including the final minimum-size commit, physical partition-tail mapping and read-only verification.
- NTFS bounded live-allocation batches, indexed GUI range application, disabled-live-map behaviour, partial-extent compaction, payload integrity and independent `ntfsresize --check` validation where NTFS utilities are available.
- GTK live range handling, operation-aware live status text and Gdk version declaration.
- Existing focused FAT12/16/32, exFAT, NTFS, EXT, Btrfs, XFS, swap, allocation mapper, Growth Defrag and GUI tests.

Physical EXT4 and Btrfs mutation still requires a real block-device partition and `CAP_SYS_ADMIN`, which this container does not provide. Shannon's removable test partitions remain the physical validation environment. Interrupted intermediate EXT4 shrink stages restore the original filesystem size; a successful Compact deliberately leaves the verified minimum-size filesystem in place. Mutation engines stop between complete journalled transactions.

## Long-suite status

The complete historical `tests/run_tests.sh` suite ran for ten minutes and reached the journal-recovery tests without reporting a failure, but it did not finish before the execution limit. The focused revision-32 tests and all directly affected backend tests completed successfully.
