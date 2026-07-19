# Linux Defragger 1.8.0 package revision 12

- Retains genuine native NTFS lowest-gap-first hole-filling compaction.
- Replaces per-cluster Python bitmap work with byte-range updates, avoiding millions of interpreter operations on large moves.
- Stops materialising one Python integer for every source and destination cluster when journalling or recovering a move.
- Batches up to 128 lower free extents and up to 262,144 clusters into one crash-safe NTFS transaction, greatly reducing journal and `fsync` overhead on volumes containing many small white gaps.
- Uses cached MFT scan metadata while selecting source extents and rereads only the finally selected MFT record, rather than rereading thousands of candidates for each gap.
- Maintains the current high-water boundary incrementally and rescans downward only when a move actually releases the physical end of the allocation.
- Keeps the same copy-first, bitmap-reserve, MFT-switch, source-release transaction order and the same forward/rollback recovery guarantees.
- Cluster zero remains protected, unsupported NTFS objects remain immovable, and there is still no NTFS-3G runtime dependency.
