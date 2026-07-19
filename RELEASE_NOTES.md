# Linux Defragger 1.7.0

Author: Shannon Smith

## Apple filesystem backends

- Added native read-only Apple HFS allocation-bitmap mapping.
- Added native read-only HFS+ and HFSX allocation-bitmap mapping.
- HFS+ reads the catalog and extents-overflow B-trees when their special files
  are represented by the volume-header extents, providing file, directory and
  fragmentation counts plus fragmented/directory map overlays.
- Added conservative APFS container detection and geometry mapping. APFS blocks
  are marked unknown except for the container superblock until the checkpoint
  and spaceman trees are implemented.
- HFS, HFS+, HFSX and APFS advertise Analyse and Map only. No Apple filesystem
  write operation is enabled in this release.

## Safety

All Apple backends open volumes read-only and contain no mutation entry point.
Existing FAT, exFAT and Amiga write engines are unchanged.
