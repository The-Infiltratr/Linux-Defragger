# Engine design and operation contracts

## User-visible operation separation

`Analyse`, `Compact`, `Defragment` and `Growth Defrag` are independent controls.
Analyse is read-only. Compact reorganises allocation to reduce internal free-space
gaps. Defragment reorganises file allocation to reduce the number of physical
extents per file. Growth Defrag is a FAT/exFAT policy layout that deliberately
creates proportional free gaps after regular files. A backend advertises each
operation separately through the capability manifest.

The native NTFS backend enforces this separation strictly: Compact preserves
the fragment count of every moved file, while Defragment rebuilds supported
fragmented files as one contiguous extent in the highest suitable free run
anywhere on the volume.
The FAT and exFAT Compact planners deliberately move allocation from the physical tail into the lowest holes, even when that increases a file's fragment count. Growth Defrag uses separate complete-object preparation planners before rebuilding every object contiguously.

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

## FAT physical tail-fill compaction planner

Normal FAT Compact is deliberately a pure free-space compactor. It finds the
lowest free clusters below the allocation high-water mark and pairs them with
movable allocated clusters taken from the physical tail. Each selected source
cluster retains its logical predecessor and successor through the mapped FAT
transaction, so file contents and chain order remain valid even when the file
becomes more fragmented.

The planner does not prefer complete files, contiguous extents or low-fragmentation
outcomes. Its only layout goal is to eliminate internal free clusters and move
the terminal free-space boundary downward. This keeps Compact distinct from
Defragment.

### Transaction limits

`--batch-clusters` limits each tail-fill transaction. `--max-clusters` remains a
hard limit on the total number of physical cluster copies. Every batch uses the
generic mapped-cluster journal and may be recovered independently.

Growth Defrag does not call this normal Compact planner. Its preparation phase
uses a separate whole-object packer because Growth Defrag immediately rebuilds
all objects contiguously and inserts deliberate post-file gaps in phase two.

## exFAT physical tail-fill compaction planner

Normal exFAT Compact follows the same operation contract. It selects the lowest
free cluster and replaces one cluster from the highest movable file or
subdirectory chain. A NoFatChain object is converted to an ordinary FAT chain
when an individual cluster move makes it non-contiguous. The directory stream
entry or predecessor FAT link is the commit point, and the schema-2 external
journal supports both rollback before that point and forward completion after it.

The exFAT root directory, allocation bitmap and other system allocations remain
fixed barriers. Growth Defrag retains its independent complete-object preparation
pass and therefore does not call the pure Compact planner.

## FAT Growth Defrag planner

Growth Defrag is intentionally neither ordinary Compact nor ordinary Defragment.
It creates a future-growth layout of:

```
contiguous file A | proportional free gap | contiguous file B | proportional free gap
```

The requested percentage is converted to whole clusters with ceiling rounding
for each non-empty regular file. Directories receive no deliberate gap. Before any mutation, the planner first performs an idempotence preflight. It verifies whether every allocated object is already contiguous and whether every regular file is followed immediately by at least the requested number of free usable clusters. An already-correct layout returns without running Compact or rewriting any object. Extra post-file free space is accepted because reducing it would provide no benefit.

If work is required, the planner verifies that free space can hold the complete reserve plus a terminal workspace at least as large as the largest allocated file or directory chain.

Phase one runs the separate whole-object preparation packer to create a large
terminal workspace without needlessly scattering the source chains. Phase two
rescans the filesystem, orders
the FAT32 root (where applicable), directories and regular files by their current
lowest physical cluster, and calculates stable target positions. Bad clusters
are treated as fixed barriers and are never counted as growth reserve.

Placement proceeds in reverse physical order. A chain whose final destination is
already free moves directly. If the destination overlaps the current chain, the
complete object first moves into the reusable terminal workspace and then into
its final target. The current object is always fully placed before a Stop request
is honoured, so no object is intentionally left staged between clean stops.

Both the staging move and final move use the generic mapped-cluster transaction.
A crash can therefore leave at most one normal external journal; Recover finishes
that transaction idempotently. A subsequent Growth Defrag run rescans and
recalculates the complete plan rather than trusting stale byte offsets.

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
It allocates the largest files first and selects the highest suitable contiguous
free run from the complete volume map. All source extents are copied in logical
order to one contiguous destination. Source holes are not returned to the
destination pool during that pass, keeping Defragment separate from Compact.
An occupied physical tail therefore does not disable defragmentation when a
sufficiently large internal free run exists.

### NTFS transaction order

Both operations use the same durable order: copy destination data, write the
external journal, mark the volume dirty, reserve destination bitmap ranges,
switch the MFT record, release source bitmap ranges, restore the original volume
flags and remove the journal. Recovery compares the current MFT record with the
saved before/after images to choose idempotent forward completion or rollback.


## GUI analysis cache and concurrent volumes

Each window owns an independent privileged helper and operation state. Opening another window permits a second volume to be analysed or modified while the first window continues its journalled operation. A volume is analysed automatically when selected. The returned allocation cells are cached by device path and resampled over the current drawing area; window resizing is therefore a memory-only redraw. A manual volume refresh invalidates the cache.
