# Test status

- Native NTFS synthetic relocation: high file data moved into a low free run, MFT mapping pairs updated, destination bits allocated, source bits released and SHA-256 payload retained.
- Native NTFS interrupted transaction: simulated crash after the MFT switch recovered forward with exact payload retention and restored original `$Volume`/`$MFTMirr` records.
- NTFS volume flags: a volume carrying `0x0080` compacted successfully, retained its exact `0x0080` state afterward and preserved the file payload; the true `0x0001` dirty state and an unsupported `0x0040` bit were rejected.
- Real NTFS image: a file deliberately allocated high after a temporary 400 MiB filler was moved natively to a low run; `ntfscat` returned the identical SHA-256 payload and independent `ntfsresize --check` accepted the final filesystem.
- The real-image creation and independent validation tests may use NTFS-3G developer utilities when they are available, but the installed Linux Defragger package does not call or depend on them.
- Existing FAT, exFAT, Amiga and Apple regression suites remain present and unchanged.
