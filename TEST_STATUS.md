# Linux Defragger 1.8.0-33 test status

Revision 33 adds direct regression coverage for the EXT4 post-mount filesystem check, NTFS complete-stream Compact, preservation of contiguous files when only smaller split holes exist, compaction of fragmented streams into one extent, lowest-run Defragment placement, live whole-stream movement events and recovery compatibility with older multi-extent journals.

Passed in this build environment:

- Python syntax and import checks for every GUI/backend engine.
- Native C engine compilation with the existing warning set.
- EXT4 iterative shrink/restore orchestration, including a forced `e2fsck -f -p` after every online extent-exchange mount and before the next or final minimum-size shrink.
- NTFS synthetic whole-stream Compact, directory-index movement, payload SHA-256 verification, bitmap updates, lower-run Defragment placement, forward recovery, rollback recovery and volume-flag preservation.
- NTFS bounded live-allocation batches and GUI live-map regression checks.
- Existing focused EXT, NTFS backend and native Compact ABI tests.
- Real-image NTFS tests are retained and automatically run when `mkntfs`, `ntfscp`, `ntfscat` and `ntfsresize` are installed; those independent utilities are unavailable in this container, so those tests reported a clean skip here.

Physical EXT4 and Btrfs mutation still requires a real writable block-device partition and `CAP_SYS_ADMIN`, which this container does not provide. Shannon's test partitions remain the physical validation environment. The specific revision-32 EXT4 failure was nevertheless reproduced from its command sequence and corrected by enforcing the e2fsprogs check required after the private online packing mount.
