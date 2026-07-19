# Linux Defragger 1.8.0-30 test status

Validated in this build environment:

- EXT4 offline Compact command sequence: forced check, minimum shrink, exact-size restore, final check and automatic size restoration after an interrupted shrink.
- Native NTFS partial-extent packing, including one-cluster gaps, mapping-pair growth limits, multiple journalled slice moves, payload verification and recovery.
- Native NTFS directory `$INDEX_ALLOCATION` relocation and map classification of protected NTFS metadata.
- Existing FAT Growth Defrag blocker evacuation, FAT/exFAT compact and growth-layout tests.
- Existing Btrfs resize planning, EXT/XFS collector planning, allocation backends, GUI dispatch and version checks.
- Python syntax checks and native engine build.

The container cannot mount loop devices or perform destructive ext4/Btrfs/XFS block-device tests, and it does not provide the external e2fsprogs executables. The EXT4 physical shrink-and-restore path is therefore covered with mocked command/geometry tests here and requires validation on the removable test volume. Real NTFS tests run automatically when `mkntfs`, `ntfscp`, `ntfscat` and `ntfsresize` are available; otherwise those independent-tool checks report that they were skipped.
