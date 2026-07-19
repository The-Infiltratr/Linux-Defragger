# Linux Defragger 1.6.0

## Amiga OFS/FFS support

- Replaces the signature-only Amiga backend with exact bitmap and directory-tree analysis.
- Reports file, directory, fragmented-file and fragmented-directory counts for DOS\0 through DOS\7.
- Adds offline Compact, Defragment and Recover operations for ADF/HDF images and raw AFFS partitions.
- Supports OFS, FFS, international, directory-cache and long-name variants.
- Relocates file headers, extension blocks and data blocks into contiguous runs.
- Relocates subdirectory headers and directory-cache chains while repairing child parent references.
- Uses an external phase journal with rollback before the parent-link switch and forward recovery after it.
- Bundles the GPL-2.0-or-later amitools filesystem library; no separate package is required.

The Amiga root block and bitmap metadata remain fixed. Gzip-compressed images are read-only.
