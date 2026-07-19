# Linux Defragger 1.8.0 package revision 15

- Adds a separate FAT-only **Growth Defrag** operation and GTK button.
- The default GUI operation leaves a 10 percent cluster-rounded expansion gap after every non-empty regular FAT file.
- Adds the native FAT `growth-defrag` command and `--growth-percent 1..25` option.
- Growth Defrag first performs normal FAT compaction, then preserves physical object order and rebuilds files/directories as contiguous chains with deliberate free gaps after regular files.
- Uses a reusable terminal workspace for overlap-safe placement and the existing mapped-cluster journal for every staging and final-placement transaction.
- Refuses the operation before mutation when free space cannot hold both the requested reserve and a workspace as large as the largest allocated object.
- Adds a distinct `CAP_GROWTH_DEFRAG` backend capability; FAT12, FAT16 and FAT32 advertise it, while other filesystems do not.
- Adds FAT12, FAT16 and FAT32 destructive Growth Defrag tests covering exact payload preservation, contiguous chains and the requested post-file gaps.
- Updates the title bar, GUI build label, native FAT engine version and NTFS engine version to `1.8.0-15`.
- Revises README, backend ABI, design notes, desktop metadata, comments and test status for the new operation.
