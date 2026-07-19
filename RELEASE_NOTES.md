# Linux Defragger 1.8.0

- Added journalled Compact, Defragment and Recover support for classic HFS.
- Added journalled Compact, Defragment and Recover support for HFS+ and HFSX ordinary data and resource forks.
- Added exact classic-HFS file and fragmentation counts through the bundled HFS scanner.
- Corrected HFS+ fragmentation detection so adjacent extent descriptors count as one physical fragment.
- Added Apple-engine routing through the persistent privileged helper.
- Added committed-transaction live map refreshes for HFS+ and HFSX.
- Bundled and statically linked hfsutils 3.2.6; no installed HFS utility is required.
- APFS remains Analyse/Map only.
