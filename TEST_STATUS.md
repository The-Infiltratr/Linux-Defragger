# Test status

- Native NTFS synthetic relocation: a high ordinary file moved into lower space, its MFT mapping pairs and `$Bitmap` were updated, the payload SHA-256 was retained, and the allocation boundary fell.
- Multi-extent synthetic relocation: no single lower hole could contain the source extent; it was split across three lower extents, the original high clusters were released and the logical payload hash remained exact.
- MFT-record growth: a mapping-pairs array was expanded from its original attribute capacity by shifting the following attribute region within the same MFT record.
- Multi-extent interrupted transactions: simulated crashes before and after the MFT mapping switch recovered backward and forward respectively with exact payload retention and restored clean `$Volume`/`$MFTMirr` records.
- High-water blocker regression: a synthetic `$MFTMirr` above a movable ordinary file was identified before writing; zero files moved and the report named `$MFTMirr $DATA`.
- Real formatted NTFS contiguous-hole image: the highest ordinary file moved natively by 8 MiB, the boundary fell by 2,045 clusters, `ntfscat` returned the identical SHA-256 payload, and independent `ntfsresize --check` accepted the final filesystem.
- Real formatted NTFS split-hole image: a 250 MiB fragmented file had no single lower hole large enough. Its high extents were relocated through multiple transactions into several lower runs, the MFT data attribute grew safely, `ntfscat` returned the identical SHA-256 payload, and independent consistency validation passed.
- NTFS volume-flag tests accept and preserve the observed non-dirty `0x0080` state while rejecting the genuine dirty bit and unknown unsafe flags.
- The real-image creation and independent validation tests may use NTFS-3G developer utilities when available, but the installed Linux Defragger package neither calls nor depends on them.
- Existing FAT, exFAT, Amiga, Apple, EXT, swap and allocation-map regression suites remain present.
