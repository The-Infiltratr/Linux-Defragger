# Engine design and operation contracts

## User-visible operation separation

`Analyse`, `Compact` and `Defragment` are independent controls. Analyse is
read-only. Compact reorganises allocation to reduce internal free-space gaps.
Defragment reorganises file allocation to reduce the number of physical extents
per file. A backend advertises each operation separately through the capability
manifest.

The native NTFS backend enforces this separation strictly: Compact preserves
the fragment count of every moved file, while Defragment rebuilds supported
fragmented files as one contiguous extent in the trailing end-of-volume area.
The older FAT planner may still reduce fragmentation as a side effect of moving
a complete chain; that existing FAT behaviour is documented below and is not
used by the NTFS planner.

## Filesystem invariants

The scanner claims every cluster reached from the FAT32 root, every subdirectory, and every regular file. A duplicate claim is a cross-link and aborts the operation. Any allocated non-bad cluster not reached from the directory tree is an orphan and also aborts.

Regular-file chain length must match the cluster count implied by the directory entry's byte size. FAT mirrors must agree for normal analysis or mutation. Recovery mode tolerates an interrupted mirror update and rewrites the entries recorded by the journal.

## Whole-file defragmentation

The directory entry is the commit pointer. A destination chain is copied and linked while the old chain remains authoritative. The short directory entry is then switched, after which recovery keeps the destination and frees the source.

## Buffered extent I/O

The native FAT engine converts a cluster mapping into source and destination extent lists. Consecutive physical source clusters that also occupy consecutive logical buffer positions form one read extent. Source extents are sorted by physical offset and read into an aligned RAM buffer; several workers may claim independent read extents when the target is non-rotational or the user explicitly requests them. Each extent writes into its predetermined buffer offset, so physical read ordering cannot alter file byte order.

Destination extents are generated from the same logical mapping and written in physical order after all source reads for the RAM chunk complete. Defragmenting a file into a contiguous run therefore normally produces one destination extent. Very large files or transactions are divided into cluster-aligned chunks bounded by `--ram-buffer`; source and destination allocation sets are disjoint, so chunking cannot overwrite unread source data.

RAM is an accelerator, not persistent recovery storage. Device data is still flushed before the journal advances from prepared to data-copied, and the old chain remains authoritative until the normal metadata switch.

Changed FAT entries are accumulated in memory, converted to the set of affected FAT sectors, and written in coalesced sector runs to every mirrored FAT or the selected active FAT. The in-memory FAT retains the original reserved upper four bits for every entry. Existing device `fsync()` boundaries remain unchanged.

Automatic worker selection deliberately uses one source reader on rotational media to avoid seek amplification. Non-rotational media uses up to eight source readers. Destination writes remain ordered and single-streamed.

## Multi-file defrag transactions

Regular-file defragmentation uses the mapped-cluster transaction when `--transaction-files` is greater than one. Files remain sorted by fragment count and size. During batch planning, each selected file receives a genuinely free contiguous run, and a temporary reservation bitmap prevents a later file in the same batch from selecting an overlapping destination.

The batch journal contains the source-to-destination mapping for every cluster of every selected file plus each file's directory-entry reference. The normal mapped transaction copies all payload data, creates translated destination chains, patches all first-cluster references, and only then frees every old chain. Recovery therefore finishes the complete batch without needing to identify individual file commit points.

Batching reduces device flush barriers from approximately four sets per file to one set per batch. Because mappings are stored in logical file order, adjacent destination runs can also become one larger destination extent in the buffered I/O layer. `--transaction-files 1` selects the original directory-entry-as-commit-pointer journal for conservative compatibility testing.

## Generic mapped-cluster transaction

Compaction and directory relocation use a journal containing a disjoint source-to-destination cluster map. For each moved cluster the journal records:

- source and destination cluster numbers;
- the source cluster's original FAT successor;
- its original FAT predecessor;
- final offsets and old/new values for affected directory references;
- the old and new root directory cluster.

When a directory cluster moves, directory-entry patch offsets are translated to the destination cluster before the journal is written. Recovery therefore does not need to rescan an inconsistent intermediate filesystem.

Destination FAT links are translated through the batch mapping. If source cluster `A` originally points to source cluster `B`, the destination for `A` points directly to the destination for `B`. External predecessors are updated only when the predecessor is not itself in the batch.

Every directory entry that refers to a moved first cluster is patched. This includes ordinary parent entries, a directory's `.` entry, child `..` entries, and any other valid short entry found by the scanner. If the FAT32 root moves, both the primary and backup boot-sector `BPB_RootClus` fields are changed.

## Forward-only recovery

Mapped-cluster recovery always completes the recorded mapping rather than trying to infer a rollback point.

Source FAT entries are freed only after destination data, destination FAT entries, predecessor links, directory references, and root pointers are durable. If interruption occurs during source freeing, source sectors still contain their bytes and cannot be reused because the external journal blocks a normal run. Recovery can safely recopy all sources and repeat every metadata write idempotently before finishing the frees.

## Durable boundaries

The journal is written through a temporary file, flushed, atomically renamed, and followed by a parent-directory flush. Device `fsync()` calls separate payload copy, metadata switch, and source release.

## FAT low-fragmentation compaction planner

The 0.4 planner is deliberately different from the 0.2 highest-cluster-to-lowest-hole algorithm.

### Whole-chain packing

The planner finds the first free run below the allocation high-water mark. It chooses complete file or directory chains that:

- fit in the remaining free run;
- lie entirely above their proposed destination;
- have disjoint source and destination sets.

Candidates nearest to the hole are preferred, preserving approximate physical order. Multiple complete objects can be included in one mapped transaction. A fragmented source chain is mapped in logical chain order to consecutive destinations, so the compaction move also defragments that object.

### Terminal staging

A one-cluster hole immediately before an eight-hundred-cluster contiguous file cannot receive the whole file directly because the destination overlaps the source. Moving one cluster at a time would be slow, while taking clusters from the far end would scatter unrelated chains.

When the object immediately following the hole is physically contiguous, the planner temporarily relocates the complete object into the terminal free run. Its old allocation merges with the low hole. A subsequent whole-chain packing transaction then fills the enlarged hole with complete objects, eventually returning the staged object to the packed region. This provides overlap-safe whole-object movement using the terminal free area as scratch space.

### Ordered-extent fallback

If no whole chain fits and the immediate object cannot be staged, the planner shifts the next contiguous physical allocation extent downward without changing cluster order. The fallback never reverses an extent and never pairs the lowest hole with an unrelated highest cluster.

Stable downward shifting preserves adjacency inside every selected physical extent. It can leave pre-existing logical fragmentation where a fragmented chain crosses extent boundaries, but it avoids the large fragmentation increase caused by arbitrary high-to-low pairing.

### Transaction limits

`--batch-clusters` limits ordered-extent fallback transactions. A whole object may exceed that soft limit because splitting an otherwise movable complete chain would create avoidable fragmentation. `--max-clusters` remains a hard limit on the total number of physical cluster copies, including temporary staging copies.

## FAT directory-chain defragmentation

Directory relocation reuses the mapped-cluster transaction. Destinations may be either below or above sources as long as the source and destination sets are disjoint and every destination is free.

The scanner records every ordinary short directory entry with a nonzero first-cluster field, including `.` and `..`. When a fragmented directory chain is mapped into a contiguous run, the journal translates patch offsets that reside inside moved directory clusters and changes every reference whose target is the moved first cluster. This repairs the parent entry, the moved directory's own `.` reference and immediate child `..` references in one transaction. The root directory is represented by `BPB_RootClus`, so both primary and backup boot sectors are patched when its first cluster moves.

Directories are moved one at a time. The filesystem is fully rescanned after each move, and it is rescanned again before regular-file defragmentation. Compaction likewise rescans between mapped transactions. This trades scanning time for auditable correctness: no operation uses a directory-entry byte offset calculated before its containing directory moved.

## Directory-sector patch coalescing

Mapped transactions may contain multiple directory-entry first-cluster patches. The journal still records each patch independently so recovery semantics and compatibility are unchanged. During the switch stage, patches are sorted by physical sector. Each affected sector is read once, all 32-byte entry changes are applied in RAM, and the sector is written once. Conflicting patches to the same entry are rejected. The switch-stage `fsync()` remains in the same position.

## Native NTFS mutation engine

The NTFS writer handles ordinary unnamed, non-resident, uncompressed,
non-sparse and non-encrypted data streams stored in one base MFT record. It
updates mapping pairs, `$Bitmap`, `$Volume` and `$MFTMirr` directly and does not
depend on NTFS-3G at runtime.

### NTFS Compact

The planner finds the lowest internal free run and searches higher supported
streams for one complete physical extent that fits. The replacement is rejected
if it would split an extent, join logical neighbours, change the stream's
physical fragment count or exceed the existing MFT record's mapping-pair
capacity. One complete extent is copied per journalled transaction.

### NTFS Defragment

The defragmenter scans for supported streams with more than one physical extent.
It allocates the largest files first from the trailing free run, working from
the physical end downward. All source extents are copied in logical order to one
contiguous destination. Source holes are not returned to the destination pool
during that pass, keeping Defragment separate from Compact.

### NTFS transaction order

Both operations use the same durable order: copy destination data, write the
external journal, mark the volume dirty, reserve destination bitmap ranges,
switch the MFT record, release source bitmap ranges, restore the original volume
flags and remove the journal. Recovery compares the current MFT record with the
saved before/after images to choose idempotent forward completion or rollback.
