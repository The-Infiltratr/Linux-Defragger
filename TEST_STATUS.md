# Linux Defragger 1.5.6 test status

- C engine compiled with warnings enabled and reports version 1.5.6.
- Python GUI, mapper and exFAT backend passed bytecode compilation.
- Exact packaged exFAT mapper test reported one fragmented file before relocation and zero afterward.
- exFAT file and directory counts match FAT-family summary semantics, including the root directory.
- Fragmented exFAT clusters are overlaid in red and directory clusters in purple.
- The exact Debian package was inspected and reports package version 1.5.6.
