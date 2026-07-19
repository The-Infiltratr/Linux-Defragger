# Linux Defragger 1.8.0

- Corrected Linux swap analysis: active usage and free-space totals now come from `/proc/swaps`, inactive swap areas report all usable pages free, bad pages remain reserved, and physically unknown active slot locations are displayed as unknown rather than free.
- Swap now reports usage rather than file allocation and explicitly marks fragmentation as not applicable.
- Added read-only ext2/ext3/ext4 inode and extent-tree scanning so Analyse reports file and directory fragmentation and marks fragmented extents on the allocation map.
- Analyse and allocation-map operations now work on mounted volumes using read-only access; write operations still require an unmounted volume.
- Mounted analysis is labelled as a live snapshot because filesystem activity can change the result during a scan.
- Added journalled Compact, Defragment and Recover support for classic HFS.
- Added journalled Compact, Defragment and Recover support for HFS+ and HFSX ordinary data and resource forks.
- Added exact classic-HFS file and fragmentation counts through the bundled HFS scanner.
- Corrected HFS+ fragmentation detection so adjacent extent descriptors count as one physical fragment.
- Added Apple-engine routing through the persistent privileged helper.
- Added committed-transaction live map refreshes for HFS+ and HFSX.
- Bundled and statically linked hfsutils 3.2.6; no installed HFS utility is required.
- APFS remains Analyse/Map only.
