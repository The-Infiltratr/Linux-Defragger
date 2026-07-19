# Linux Defragger 1.8.0 package revision 16

- Corrects Growth Defrag safe-stop reporting during the preparation/compaction phase.
- An interrupted preparation phase is now reported as **stopped safely**, not as **phase complete**.
- The summary now states explicitly that the growth-space layout was not started and that no expansion gaps were applied.
- An interruption during the layout phase is reported separately as a partial layout, including the number of complete objects and transactions finished before the stop.
- Native mutating operations now return exit status `130` after a clean SIGINT stop, allowing the GUI to distinguish a safe stop from success or failure.
- The GTK interface shows **Stopped safely**, refreshes the allocation map, and preserves the safe-stop status after the refresh instead of reporting the interrupted operation as completed.
- Updates the title bar, GUI build label, native FAT engine version and NTFS engine version to `1.8.0-16`.
