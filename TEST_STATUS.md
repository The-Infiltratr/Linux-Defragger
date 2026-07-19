# Test status

- Native NTFS synthetic relocation: high file data moved into a low free run, MFT mapping pairs updated, destination bits allocated, source bits released and SHA-256 payload retained.
- Native NTFS interrupted transaction: simulated crash after the MFT switch recovered forward with exact payload retention and restored clean `$Volume`/`$MFTMirr` records.
- Real NTFS image: a file deliberately allocated high after a temporary 400 MiB filler was moved natively to a low run; `ntfscat` returned the identical SHA-256 payload and independent `ntfsresize --check` accepted the final filesystem.
- The real-image creation and independent validation tests may use NTFS-3G developer utilities when they are available, but the installed Linux Defragger package does not call or depend on them.
- Existing FAT, exFAT, Amiga and Apple regression suites remain present and unchanged.
