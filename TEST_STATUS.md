# Test status

- Native NTFS synthetic hole filling: movable file data filled the lowest internal gaps even when an immovable `$MFTMirr` remained at the physical high-water boundary; payload SHA-256 and bitmap state remained exact.
- Synthetic complete packing: three separated lower holes were filled in ascending physical order and all free clusters below the final allocation boundary were eliminated.
- NTFS boot protection: cluster zero is excluded from destination planning even when a synthetic allocation bitmap incorrectly marks it free.
- Multi-extent interrupted transactions: simulated crashes before and after the MFT mapping switch recovered backward and forward respectively with exact payload retention and restored clean `$Volume`/`$MFTMirr` records.
- Real formatted NTFS contiguous-hole image: supported file extents filled lower gaps, the allocation boundary fell, `ntfscat` returned the identical SHA-256 payload, and independent `ntfsresize --check` accepted the final filesystem.
- Real formatted NTFS split-hole image: multiple lower holes were filled through several transactions, MFT mapping-pair growth remained valid, the payload SHA-256 was exact, and independent consistency validation passed.
- NTFS volume-flag tests accept and preserve the observed non-dirty `0x0080` state while rejecting the genuine dirty bit and unknown unsafe flags.
- The real-image creation and independent validation tests may use NTFS-3G developer utilities when available, but the installed Linux Defragger package neither calls nor depends on them.
- Existing FAT, exFAT, Amiga, Apple, EXT, swap and allocation-map regression suites remain present.
