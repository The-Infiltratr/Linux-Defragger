# Test status

- Native NTFS synthetic relocation: a high ordinary file was moved into a low free run, its MFT mapping pairs and `$Bitmap` were updated, the payload SHA-256 was retained, and the boundary fell from cluster 3,515 to cluster 100.
- High-water blocker regression: a synthetic `$MFTMirr` above a movable ordinary file was identified before writing; zero files moved, the ordinary file mapping and payload stayed unchanged, and the report named `$MFTMirr $DATA`.
- Native NTFS interrupted transaction: a simulated crash after the MFT switch recovered forward with exact payload retention and restored clean `$Volume`/`$MFTMirr` records.
- Real formatted NTFS image: the highest ordinary file moved natively by 8 MiB, the allocation boundary fell by 2,045 clusters, the next blocker was identified as `$LogFile $DATA`, `ntfscat` returned the identical SHA-256 payload, and independent `ntfsresize --check` accepted the final filesystem.
- NTFS volume-flag tests still accept and preserve the observed non-dirty `0x0080` state while rejecting the genuine dirty bit and unknown unsafe flags.
- The real-image creation and independent validation tests may use NTFS-3G developer utilities when available, but the installed Linux Defragger package neither calls nor depends on them.
- Existing FAT, exFAT, Amiga, Apple, EXT, swap and allocation-map regression suites remain present.
