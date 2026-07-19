# Linux Defragger 1.8.0 package revision 14

- Fixes native NTFS Defragment incorrectly requiring free space beyond the current allocation boundary.
- NTFS Defragment now searches the complete volume free-space map and chooses the highest suitable contiguous run anywhere on the volume.
- A fully occupied physical tail no longer prevents defragmentation when sufficiently large internal free runs exist.
- Defragment still does not reuse source holes released during the same pass, preserving its separation from Compact.
- Updates NTFS progress and completion messages to describe whole-volume destination selection rather than a trailing-only destination area.
- Adds a destructive regression test for a fragmented NTFS stream on a volume with an occupied physical tail and usable internal free space.
- Updates the title bar, build label, native FAT engine version and NTFS engine version to display `1.8.0-14`.
- Updates README and design documentation to match the corrected NTFS destination planner.
