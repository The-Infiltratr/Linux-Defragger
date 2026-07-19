# Linux Defragger 1.8.0 package revision 19

- Makes the FAT Growth Defrag preflight visible on every run before any journal or filesystem write is created.
- Reports the exact first reason a layout needs work, including a fragmented object or a file with fewer usable post-file reserve clusters than requested.
- Adds a second independent canonical-layout verifier for layouts previously produced by Growth Defrag.
- Includes the complete native engine revision in the preflight line, making stale or mismatched installed engines immediately visible in operation logs.
- Keeps an already-correct layout read-only and reports **Not needed** with no preparation compaction or relocation.
- Retains RAM-backed batching, SD/eMMC worker selection, quiet normal logging, UTF-8 FAT names and safe-stop handling.
- Updates the title bar, GUI build label, native FAT engine version and NTFS engine version to `1.8.0-19`.
