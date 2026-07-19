# Test status for 1.8.0-16

- Both C engines compile cleanly with the normal warning set.
- Python syntax compilation passes for the GTK GUI, privileged helper, backend modules, native filesystem engines and test scripts.
- Normal FAT12, FAT16 and FAT32 Growth Defrag tests pass, including exact payload preservation, contiguous final chains and the requested post-file expansion gaps.
- A destructive interrupted-preparation test confirms the engine exits with status `130`, removes the active journal, leaves the filesystem readable and reports **Stopped safely during preparation**.
- The interrupted-preparation output no longer says `phase 1 complete`, no longer claims a zero-percent reserve was applied, and explicitly reports that the layout was not started and no expansion gaps were applied.
- The GTK wiring test confirms status `130` is handled as **Stopped safely**, not as success or failure, and that the safe-stop status is restored after the allocation-map refresh.
- Mounted-analysis policy, allocation mapper, EXT, NTFS analysis and swap backend focused tests pass.
- The complete long-running regression script reached the 20-minute execution limit during unchanged FAT compaction coverage without reporting a failure before it was stopped. It did not complete in this build environment.
- Real physical FAT media have not been used for this revision; validation was performed on controlled destructive filesystem images.
