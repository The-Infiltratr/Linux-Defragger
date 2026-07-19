# Test status

- Native NTFS synthetic split-hole test now fills three separate lower gaps in one journalled transaction while preserving the exact payload and allocation bitmap.
- Native NTFS forward and rollback recovery passed using range-based bitmap updates.
- Real formatted NTFS contiguous-hole relocation passed with identical SHA-256 payload and independent consistency validation.
- Real formatted NTFS split-hole relocation moved 252.2 MiB in three transactions, reduced the boundary by 380.2 MiB, retained the exact SHA-256 payload and passed independent validation.
- NTFS allocation/MFT fragmentation analysis and mounted read-only analysis policy tests passed.
- Python syntax compilation passed for the packaged NTFS engine.
- Existing FAT, exFAT, Amiga, Apple, EXT, swap and allocation-map regression suites remain present.
