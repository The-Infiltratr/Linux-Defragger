# Linux Defragger 1.5.6

- exFAT analysis now reports regular files, directories, fragmented files and fragmented directories.
- The exFAT pixel map now overlays fragmented clusters in red and directory clusters in purple.
- The GUI summary and operation log now use the same fragmentation-count presentation as FAT12/16/32.

Author: Shannon Smith

## Fixes

- Corrects the exFAT summary card and status line.
- The GUI now derives wording from backend capability flags rather than assuming every `read-only-domain` map is a read-only filesystem.
- Writable exFAT volumes now display `Map · Compact / Defragment / Recover`.
- NTFS and other analysis-only backends continue to display `Map only · read-only`.
- No filesystem movement, journal or recovery algorithms changed.
