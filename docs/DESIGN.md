# Linux Defragger architecture and operation contracts

## Layered architecture

Linux Defragger is divided into five layers.

1. **GTK presentation** — `linux_defragger_gui.py` displays volumes, maps, status and logs. It consumes manifests and does not choose filesystem-specific mutation workers.
2. **Operation engine** — `operation_engine.py` is the single mutation dispatcher. It resolves the selected filesystem plugin and executes the worker declared by that plugin.
3. **Shared core modules** — `gui/core` owns executable paths, standard operation arguments, mount-state checks and durable JSON-journal writes.
4. **Filesystem plugins** — `gui/backends` owns signature probing, read-only allocation analysis, capabilities and standardized operation declarations.
5. **Mutation workers** — the C FAT engine and filesystem-specific Python/C workers contain the on-disk relocation algorithms. They remain independently executable and testable.

The privileged helper is intentionally not a filesystem layer. It accepts a small fixed protocol and launches only the read-only mapper or central operation engine rather than maintaining a second filesystem dispatch table.

## Operation separation

`Analyse`, `Compact`, `Defragment` and `Growth Defrag` are separate operations.

- Analyse is read-only and may inspect a mounted snapshot.
- Compact reduces internal free-space gaps without being allowed to introduce fragmentation on filesystems whose plugin promises whole-stream movement.
- Defragment rebuilds fragmented supported files as contiguous allocations, using the lowest suitable destination unless temporary staging is required.
- Growth Defrag is a FAT/exFAT policy layout that creates proportional post-file reserve space.

A plugin advertises each operation explicitly. The engine never infers Defragment from Compact or Recover from another capability.

## Shared map processing

All analysis plugins emit the same allocation-map schema. Raw reads use the shared `Reader`, which loops until a complete positional request is satisfied rather than assuming one `pread()` returns the whole span.

Range utilities use half-open intervals and central validation. State ranges cannot overlap. Uncovered spans become unknown. Allocation aggregation advances monotonically through both map cells and sorted ranges, reducing the main mapping path from repeated range scans to linear traversal.

## Standard mutation dispatch

The GUI builds only operation-wide policy arguments such as RAM budget, worker policy, transaction size and live-map resolution. It invokes:

```text
operation_engine.py OPERATION DEVICE --filesystem PLUGIN [standard arguments]
```

The selected plugin declares:

- worker ID;
- user-visible label, description and warning;
- whether the canonical filesystem ID must be forwarded;
- any standard option unsupported by that worker.

The operation engine resolves a fixed executable path, filters only the explicitly unsupported standard options and replaces itself with the worker process. This preserves direct signal delivery and exit status while keeping the GUI and privileged helper filesystem-neutral.

## Recovery and durable boundaries

Mutation workers retain their existing filesystem-specific transaction semantics. Shared JSON journal replacement uses a private temporary file, `fsync()` of the journal, atomic rename and parent-directory `fsync()`.

FAT mapped-cluster recovery completes recorded mappings forward. NTFS examines the active MFT mapping and either completes or rolls back its stream transaction. Other workers preserve their existing transaction-specific recovery rules.

## Filesystem-specific mutation invariants

FAT scanners claim every reachable cluster and abort on cross-links or unreferenced allocation. Whole-file Defragment keeps the original directory entry authoritative until destination data and FAT chains are durable. Compact and Growth Defrag use separate planners.

NTFS Compact relocates complete supported streams into lower contiguous gaps and does not split a stream merely to consume a small hole. NTFS Defragment rebuilds a stream as one contiguous extent in the lowest suitable free run; higher space is staging only.

EXT4 Compact uses offline checking and minimum-size shrink rounds, with regular-file low-hole packing between shrink rounds. XFS Compact uses complete-file allocation swaps and rejects layouts that increase the source file's extent count. Btrfs Compact works at chunk level through balance and shrink/restore because arbitrary copy-on-write extents cannot be physically exchanged safely.

## Adding a filesystem

A new read-only plugin requires:

1. a module implementing `FilesystemBackend`;
2. a validated `BackendInfo` declaration;
3. a `BACKEND` instance;
4. one entry in `PLUGIN_MODULES`;
5. focused probe/map tests.

A mutation-capable plugin additionally declares one or more `OperationSpec` records. A genuinely new worker receives one fixed ID in `core/paths.py`; no GUI or privileged-helper filesystem branch is added.
