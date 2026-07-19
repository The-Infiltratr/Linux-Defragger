# Linux Defragger 1.8.0 package revision 8

- Corrected native NTFS `$VOLUME_INFORMATION` validation. Revision 7 incorrectly treated every non-zero volume flag as the dirty state.
- NTFS dirty detection now checks only the defined `0x0001` dirty bit.
- The observed undocumented `0x0080` flag is accepted and preserved exactly while Linux Defragger temporarily adds and removes its transaction dirty bit.
- Active NTFS maintenance states and genuinely dirty volumes remain blocked. Other unrecognised flag bits remain conservatively rejected.
- Added destructive relocation and recovery regression coverage for a volume carrying `0x0080`, including exact post-operation flag restoration and SHA-256 payload retention.
