# Linux Defragger 1.8.0 package revision 10

- Replaced whole-file contiguous NTFS relocation with high-water extent relocation.
- A large file no longer needs one lower free run equal to its complete size.
- The engine can move only the extent or suffix that owns the final allocated cluster.
- One logical source extent can be copied into several lower physical free extents in a single journalled transaction.
- Mapping pairs are rebuilt while preserving the file's logical VCN order.
- When the new mapping pairs need more room, the data attribute is expanded safely within unused space in the existing MFT record; `$ATTRIBUTE_LIST` creation remains unsupported and is not attempted.
- Recovery journal schema 3 records the exact released and destination extents. Revision 10 can still recover schema-2 journals created by revisions 7 through 9.
- Corrected NTFS mapping-pair run-length encoding so positive lengths with the high bit set receive the required sign-preserving zero byte.
- Added multi-extent forward-recovery and rollback tests plus an independently formatted real NTFS split-hole test.
- Python bytecode generation is disabled by the launcher, and package upgrade cleanup removes stale `__pycache__` directories.
- There remains no NTFS-3G runtime dependency.
