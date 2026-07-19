# Linux Defragger 1.8.0 package revision 18

- Makes FAT Growth Defrag idempotent. Before compaction, the engine now checks whether all allocated objects are already contiguous and every regular file already has at least the requested post-file reserve.
- Returns **Not needed** with zero filesystem writes when an existing 10% Growth Defrag layout is already satisfactory.
- Accepts extra free space after a file rather than compacting it away merely to reproduce an exact percentage.
- Adds a byte-for-byte regression test proving that a second Growth Defrag pass leaves an already-correct FAT image unchanged.
- Updates the GTK completion state to show **Not needed** after the refreshed allocation map rather than claiming that a redundant relocation completed.
- Retains the RAM-backed batching, SD/eMMC worker policy, quiet logging, UTF-8 FAT names and safe-stop behaviour from revision 17.
- Updates the title bar, GUI build label, native FAT engine version and NTFS engine version to `1.8.0-18`.
