# Linux Defragger 1.5.3

- Restores the required `shutil` import removed during the 1.5.2 cleanup.
- Fixes launch-time PolicyKit authentication and privileged FAT analysis.
- Surfaces startup authentication failures instead of leaving operations stuck at “in progress”.
- No filesystem engine or on-disk write logic changed.

# Linux Defragger 1.5.3

This release removes unpublished compatibility baggage and standardises the entire project on the Linux Defragger name.

- One launcher: `/usr/bin/linux-defragger`.
- One engine: `/usr/bin/linux-defragger-engine`.
- One private runtime directory: `/usr/lib/linux-defragger`.
- One state directory: `~/.local/state/linux-defragger`.
- Removed the `liux-defragger`, `fat32defrag-gui`, and `fat32defrag` command aliases.
- Removed legacy desktop entries, icons, v0.7/v0.8 binary searches, and old environment-variable names.
- Renamed the C target, JSON program identifier, journal prefix, GUI module, and internal paths.
- Fixed the privileged helper's progress parser, which had been left permanently disabled by a stale placeholder assignment.
- Filesystem algorithms and backend capabilities are otherwise unchanged from 1.5.1.
