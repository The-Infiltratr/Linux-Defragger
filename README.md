# Linux Defragger 1.5.3

**Author:** Shannon Smith  

Linux Defragger is a self-contained GTK allocation-map and FAT-family defragmentation application.

## Administrator session

The GUI requests administrator authentication immediately after launch. A private privileged helper remains alive for the GUI session and is reused by Analyse, Compact, Defragment, Recover, Unmount, and automatic refresh operations. Closing the GUI terminates the helper.

## Filesystem support

FAT12, FAT16, FAT32 and exFAT provide analyse, map, compact, defragment and recovery operations. NTFS and the other registered filesystems remain read-only analysers unless their backend explicitly advertises mutation capabilities.

## Amiga OFS/FFS

Version 1.6.0 adds exact allocation analysis and journalled Compact, Defragment and Recover operations for Amiga DOS\0 through DOS\7 volumes. The bundled amitools filesystem library supplies validated OFS/FFS structure parsing; Linux Defragger supplies the relocation planner and external recovery journal.
