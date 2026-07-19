# Linux Defragger 1.8.0

Linux Defragger is a modular GTK allocation-map viewer and offline defragmenter.

## Write-capable backends

- FAT12, FAT16 and FAT32: analyse, map, compact, defragment and recover.
- exFAT: analyse, map, compact, defragment and recover.
- Amiga OFS/FFS variants: analyse, map, compact, defragment and recover.
- Classic Apple HFS: analyse, map, compact, defragment and recover.
- Apple HFS+ and HFSX: analyse, map, compact, defragment, recover and live map updates.

NTFS, ext2/3/4, Btrfs, XFS, UFS, ZFS, APFS, swap and Minix remain analysis backends according to their advertised capabilities.

## Apple filesystem implementation

Classic HFS uses the bundled GPLv2 libhfs source from hfsutils 3.2.6, statically linked into a private engine. It relocates complete data and resource forks, updates the volume bitmap, catalogue and extents-overflow B-tree, and uses an external recovery journal.

HFS+ and HFSX use a native Python engine. It relocates complete ordinary data and resource forks, updates allocation-file bits, inline and overflow extent descriptors, primary and alternate volume-header free counts, and uses an external recovery journal. The allocation, catalogue, extents-overflow and other special filesystem files remain fixed.

All mutation commands refuse mounted volumes. The source allocation remains allocated until the catalogue metadata durably points to the destination.

## Build

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

## Author

Shannon Smith
