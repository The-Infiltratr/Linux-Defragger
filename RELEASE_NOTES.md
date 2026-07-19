# Linux Defragger 1.8.0 package revision 11

- Replaced the NTFS high-water-only planner with genuine lowest-gap-first compaction.
- Compact now fills the earliest free extent inside the allocated region using supported file extents from higher physical locations.
- An immovable object at the physical end no longer prevents lower holes from being filled by unrelated movable files.
- Whole source extents are preferred to minimise additional fragmentation; safe suffix splitting is used only when a whole extent cannot fill the current gap.
- The packed prefix advances monotonically, driving free space toward the physical end of the NTFS volume.
- Progress is based on movement of the lowest remaining internal gap rather than only on high-water reduction.
- The final report includes the number and size of free gaps remaining below the allocation boundary and names the first gap that cannot be filled.
- Cluster zero is independently protected as the NTFS boot-sector cluster even if a malformed or synthetic `$Bitmap` incorrectly marks it free.
- Transaction journalling, forward recovery, rollback recovery, volume dirty-state handling, multi-extent mapping pairs and MFT-record growth remain unchanged.
- There remains no NTFS-3G runtime dependency.
