# Test status for 1.8.0-15

- Both C engines compile cleanly with the normal warning set.
- Python syntax compilation passes for the GTK GUI, privileged helper, backend modules, native filesystem engines and test scripts.
- FAT32 Growth Defrag destructive testing uses two fragmented files of 10 and 20 clusters. The final chains are contiguous at clusters 3-12 and 14-33, with one and two free clusters respectively after the files. Every payload byte is preserved.
- FAT12 and FAT16 Growth Defrag tests rebuild a fragmented three-cluster file at clusters 2-4, preserve the A/B/C payload order and leave cluster 5 free as the rounded 10 percent growth gap.
- The backend manifest test confirms only FAT backends advertise `CAP_GROWTH_DEFRAG` and that the GUI/helper route `growth-defrag` to the native FAT engine.
- The complete existing regression suite passed after the change, including FAT analysis, defragmentation, compaction, directory relocation, mapped recovery, interruption, FAT mirror and live-map tests.
- Existing NTFS, EXT, swap, Apple, allocation-mapper and mounted-analysis tests passed. Optional independently formatted NTFS tests run when their external validation utilities are available.
- Real physical FAT media have not been used for the new Growth Defrag test; validation was performed on controlled destructive filesystem images.
