# Linux Defragger 1.8.0 test status

The exact source build passed:

- Synthetic Linux swap headers with inactive accounting, active `/proc/swaps` accounting, reserved header pages, bad-page handling and unknown physical-slot maps.
- GUI unknown-state rendering and swap-specific usage/fragmentation summaries.
- Synthetic ext4 extent-tree and ext2 indirect-block images, including fragmented files, fragmented directories, file counts, percentage calculation and map overlays.
- The complete pre-existing FAT regression suite (`all tests passed`).
- HFS+ three-extent data-fork relocation with byte-for-byte payload verification.
- HFS+ pre-switch rollback at copied and destination-ready phases.
- HFS+ post-switch forward recovery.
- HFSX fragmented-fork relocation.
- Classic HFS data-fork defragmentation with independent `hfsck` validation.
- Classic HFS compaction with SHA-256 payload verification.
- Classic HFS pre-switch rollback and post-switch forward recovery, each followed by `hfsck`.
- Python compilation for all GUI and backend modules.
- Capability registry validation: HFS advertises Analyse/Map/Compact/Defragment/Recover; HFS+/HFSX additionally advertise live map updates.
- The exact unpacked Debian package repeated HFS and HFS+ mutation tests.

No physical Apple-formatted media was available. The first physical run should use backed-up, replaceable media.
## Mounted-volume analysis policy

- GUI Analyse control is enabled for mounted supported volumes.
- Native FAT read-only commands no longer request an exclusive block-device open and do not reject the active-volume dirty bit.
- Compact, Defragment and Recover retain mounted-volume rejection and exclusive writable opens.

