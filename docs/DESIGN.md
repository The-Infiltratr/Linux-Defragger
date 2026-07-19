# Engine design and operation contracts

## User-visible operation separation

`Analyse`, `Compact`, `Defragment` and `Growth Defrag` are independent controls.
Analyse is read-only. Compact reorganises allocation to reduce internal free-space
gaps. Defragment reorganises file allocation to reduce the number of physical
extents per file. Growth Defrag is a FAT/exFAT policy layout that deliberately
creates proportional free gaps after regular files. A backend advertises each
operation separately through the capability manifest.

The native NTFS backend never increases fragmentation in either operation.
Compact relocates complete streams into lower contiguous gaps and may reduce an
already fragmented stream to one extent. Defragment rebuilds supported
fragmented streams as one contiguous extent in the lowest suitable free run,
then settles any temporarily staged streams downward.
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

The planner finds the lowest internal free run and searches all higher supported
streams for the largest complete stream that fits. Every physical source extent
for that stream is copied in logical order into one contiguous destination run.
The move is rejected if any source allocation lies below the destination gap or
if the single-run replacement cannot fit the existing MFT record. Small gaps
that cannot hold a complete higher stream remain free; Compact never splits a
file to consume them.

### NTFS Defragment

The defragmenter scans for supported streams with more than one physical extent,
allocates larger streams first, and selects the lowest suitable contiguous free
run from the complete volume map. All source extents are copied in logical order
to one contiguous destination. If the only available run is higher than the
source, it acts as temporary staging. Freed source space is immediately visible
to later work, and bounded settling passes move staged contiguous streams lower
again. Every relocation is whole-stream and cannot increase fragment count.

### NTFS transaction order

Both operations use the same durable order: copy destination data, write the
external journal, mark the volume dirty, reserve destination bitmap ranges,
switch the MFT record, release source bitmap ranges, restore the original volume
flags and remove the journal. Recovery compares the current MFT record with the
saved before/after images to choose idempotent forward completion or rollback.


## Native Btrfs read-only analyser

The Btrfs backend does not use aggregate `bytes_used` accounting as a substitute
for a physical map. It reads the primary superblock, parses the system chunk
array, then walks the chunk tree to build the filesystem's logical-to-physical
translation table. The root tree identifies the extent tree and every live
filesystem root.

Allocated logical extents are obtained from extent and skinny-metadata items and
translated to every local physical mirror. The physical complement, plus fixed
superblock mirror reservations, forms the exact single-device free/used map.
Regular-file fragmentation is calculated from each inode's non-inline file-extent
items after logical and physically adjacent extents are coalesced. Directory
inodes are counted, but Btrfs directory records occupy shared filesystem-tree
blocks rather than private directory chains, so the backend does not invent a
directory-fragmentation count.

SINGLE, DUP and local same-device mirrors are supported. Multi-device chunks and
RAID0/10/5/6 layouts are rejected until a stripe-aware multi-device mapper can
represent every physical target. The backend opens the volume read-only and
advertises no mutation capabilities in the analyser itself; revision 23 adds a separate kernel-driven Compact engine.

## Native XFS read-only analyser

The XFS backend reads the primary superblock and validates data-device, allocation
group, sector and inode geometry. For each allocation group it reads the AGF and
walks the block-number free-space B+tree (`bnobt`). The merged free records are
exact physical free ranges; their data-device complement is the used allocation
map. This replaces the former aggregate summary and avoids relying on AGF counters
as positional information.

Allocated inode numbers are obtained by walking each AGI inode B+tree, including
sparse-inode records. Every allocated inode's data fork is decoded in local,
direct-extent or bmap B+tree format. Physical extent counts provide regular-file
and directory fragmentation counts and the red/purple map overlays. Realtime data
that resides on a separate realtime device cannot be positioned on the data-device
map and is reported conservatively.

The XFS analyser supports version 4 and version 5 short B+tree headers, version 5
CRC bmap blocks, sparse inodes and NREXT64 inode extent counters. It opens the
filesystem read-only and invokes no external XFS utility. Revision 23 adds a separate kernel-driven Compact engine when the running kernel supports range exchange.

## GUI analysis cache and concurrent volumes

Each window owns an independent privileged helper and operation state. Opening another window permits a second volume to be analysed or modified while the first window continues its journalled operation. A volume is analysed automatically when selected. The returned allocation cells are cached by device path and resampled over the current drawing area; window resizing is therefore a memory-only redraw. A manual volume refresh invalidates the cache.

## Native ext4 and XFS free-space compactor

The native Linux compactor does not call file-defragmentation utilities. It privately mounts an otherwise-unmounted filesystem and creates unlinked collector files that temporarily own nearly all accessible free blocks. FIEMAP on the collector is therefore the kernel-confirmed physical free map. Each low collector extent is used directly as the donor; the engine does not punch it free or attempt a second allocation after reserving the free-space map.

The highest supported regular-file extent is copied into the collector's exact logical slice. ext4 commits the mapping with `EXT4_IOC_MOVE_EXT`; XFS commits it with `XFS_IOC_EXCHANGE_RANGE`. The collector then owns the old high mapping and remains open until the end of the pass. Closing the collector set releases all retained high mappings together. This is a compaction planner, not a defragmentation planner: a partial suffix may be moved and the source file may gain another extent.

Collector and donor names are unlinked immediately. If the process terminates before an exchange, closing the descriptors restores the low hole without changing the source file. If it terminates after a successful kernel transaction, the source file already owns the low mapping and descriptor teardown releases the high mapping. The engine never writes ext4 or XFS allocation metadata directly.

## Native Btrfs chunk compactor

Btrfs file extents cannot be exchanged safely without updating copy-on-write backreferences. Compact therefore operates at the chunk layer through the mounted kernel driver. The engine reads the current chunk tree with `BTRFS_IOC_TREE_SEARCH_V2`, calculates a temporary filesystem boundary above the allocated chunk total, and calls `BTRFS_IOC_RESIZE`. Shrinking forces chunks above that boundary into lower available chunk ranges. The original filesystem size is restored immediately afterward, leaving the relocated chunks low and the released physical space at the tail.

The resize is always wrapped in restoration cleanup, including error and Stop paths. This operation can consolidate device-level chunk allocation but does not remove free space inside allocated Btrfs block groups. That inner free space belongs to Btrfs's extent allocator and is distinct from unallocated device gaps between chunks.
