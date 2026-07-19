# Test status for 1.8.0-20

Validated in controlled filesystem images:

- FAT12, FAT16 and FAT32 analysis, map output, journal recovery, file/directory defragmentation and Growth Defrag.
- FAT Compact tail filling removes all internal free clusters while retaining valid file payload order; deliberately fragmented test files remain fragmented after Compact.
- FAT Growth Defrag uses its separate whole-object preparation path and remains idempotent.
- FAT map output reports `growth_10_satisfied` for the GUI cached preflight.
- exFAT pure tail-fill Compact, forward/rollback recovery, defragmentation, Growth Defrag, payload preservation, capability advertisement and cached growth-layout status.
- GUI static regressions for unified versioning, automatic analysis, memory-only resize redraw, independent windows and fragmented test-data integration.
- Existing NTFS, EXT, swap and allocation-mapper focused tests remain present.

Physical removable media should continue to be treated as bug-testing media until repeated real-device validation is complete.

The complete automated regression suite passed for revision 20. The exact staged Debian package also passed FAT tail-fill Compact, FAT Growth Defrag idempotence, exFAT tail-fill Compact including NoFatChain conversion, exFAT Growth Defrag/cached preflight, Python compilation and fragmented test-data generation checks.
