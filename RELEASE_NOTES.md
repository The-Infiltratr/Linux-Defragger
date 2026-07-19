# Linux Defragger 1.8.0 package revision 13

- Adds native offline NTFS file defragmentation and enables the NTFS Defragment button.
- NTFS Defragment finds supported fragmented ordinary files, rebuilds each as one contiguous extent and allocates rebuilt files from the physical end of the volume downward.
- Separates NTFS Compact from Defragment. Compact now moves complete physical extents only and rejects any move that would change a file's fragment count.
- Prevents NTFS Compact from splitting one extent across several holes or accidentally joining logical neighbours.
- Retains journalled copy, bitmap reservation, MFT switch, source release, dirty-state handling and forward/rollback recovery for both NTFS operations.
- Adds File and About menus, including Open image, Refresh volumes, Quit and a GTK About dialog.
- Updates the title bar, build label, native FAT engine version and NTFS engine version to display `1.8.0-13`.
- Replaces the outdated subtitle with a direct description of Analyse, Compact and Defragment.
- Renames the old FAT-specific GTK application class to `LinuxDefraggerApplication`.
- Audits the NTFS comments and documentation so production Compact behaviour is no longer described as high-water splitting or multi-gap defragmenting compaction.
- Updates the desktop description and backend capability manifest for native NTFS Defragment support.
