# Linux Defragger 1.8.0-35 test status

Revision 35 is an architecture and code-quality release. The filesystem mutation algorithms are retained while dispatch, plugin contracts, shared services and allocation-map aggregation are standardised.

Passed in this build environment:

- Clean CMake Release build of the native FAT engine and classic HFS engine with the existing compiler warning set.
- Python syntax/import compilation for the GUI, dispatcher, helpers, workers, shared modules, plugins and tests.
- Single-source version checks for the C FAT engine, operation engine, exFAT, NTFS, Amiga and Apple workers.
- Registry validation for all 16 built-in filesystem plugins and 37 filesystem aliases.
- Capability/operation consistency and worker resolution for every mutation-capable plugin.
- Central-dispatch tests for FAT, NTFS option filtering and ext4 canonical filesystem forwarding.
- Privileged-helper and GUI checks confirming mutation dispatch goes only through the operation engine.
- Randomised equivalence tests for shared allocation-range aggregation, overlap rejection, complements and overlays.
- Synthetic 40,000-range/50,000-cell performance validation of the linear map aggregator.
- FAT Growth Defrag batching and UTF-8 long-name tests.
- Btrfs, EXT, NTFS, XFS, swap, Apple and modular allocation-mapper focused tests.
- Native Linux Compact ioctl, collector, XFS whole-file fallback, Btrfs balance/resize and EXT4 orchestration tests.
- Native NTFS Compact, Defragment and recovery tests using the repository's deterministic image generator.

Independent real-volume NTFS tests were skipped because external NTFS validation utilities are unavailable in this container. The environment also cannot mount and mutate real writable EXT4, Btrfs or XFS partitions, so physical block-device validation remains a test-machine task rather than a claim of this build report.

The complete historical `tests/run_tests.sh` suite was also run under a 20-minute limit. It reported no failure before the limit expired, but did not complete, so it is not recorded as a full-suite pass. The focused suites above cover the architecture changes and the affected native workers.
