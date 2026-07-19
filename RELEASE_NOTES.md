# Linux Defragger 1.8.0 package revision 20

- Uses one Python version source and queries the installed native engine for the visible engine revision. The title bar, upper-right build label and About dialog now agree on `1.8.0-20`.
- Automatically analyses a volume when it is selected.
- Keeps analysed allocation samples in memory and resamples them during window resizing. Resizing no longer rereads the device.
- Changes native FAT and exFAT Compact to pure physical tail filling. Compact removes internal free gaps without attempting to preserve or improve file fragmentation.
- Retains a separate whole-object preparation planner for FAT Growth Defrag, followed by the normal contiguous growth-layout phase.
- Adds exact cached `growth_10_satisfied` analysis data. Re-running Growth Defrag on an already-correct FAT or exFAT volume returns immediately from the existing analysis without a second scan or write.
- Adds native exFAT Growth Defrag and advertises the capability in the backend registry.
- Adds independent application windows. Each window owns its own authenticated helper and may operate on a different volume concurrently.
- Adds **File → Create fragmented test data…** and the `linux-defragger-testdata` command for portable cross-filesystem test generation.
- Updates comments, CLI help, README, design documentation and tests to match the separated operation semantics.
