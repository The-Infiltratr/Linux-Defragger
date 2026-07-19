# Filesystem plugin ABI — version 2

Linux Defragger separates read-only filesystem knowledge from mutation orchestration. A filesystem plugin is a Python module under `gui/backends` that exports one `BACKEND` object implementing `FilesystemBackend`.

The stable plugin surface is divided into focused modules:

- `contracts.py` defines capabilities, `BackendInfo`, `OperationSpec`, `FilesystemBackend` and `BackendError`.
- `io.py` provides robust positional raw-device reads and endian helpers.
- `ranges.py` provides shared range merging, complements, overlays and linear-time allocation-map aggregation.
- `registry.py` loads the built-in plugins and rejects duplicate IDs, aliases or invalid plugin objects.
- `base.py` remains a compatibility re-export surface. New shared implementation belongs in one of the focused modules above.

Every plugin implements:

```python
class ExampleBackend(FilesystemBackend):
    info = BackendInfo(...)

    def probe(self, path: str) -> bool:
        ...

    def map(self, path: str, cells: int) -> dict:
        ...

BACKEND = ExampleBackend()
```

`probe()` and `map()` are read-only. A plugin must never infer or invent physical allocation. Unsupported positional knowledge is represented as `unknown`, and the plugin declares an appropriate `map_accuracy` such as `exact`, `exact-single-device` or `summary`.

## Standard operations

Mutation support is declared by `OperationSpec` records inside `BackendInfo.operations`. Supported names are:

- `compact`
- `defrag`
- `growth-defrag`
- `recover`

An operation declaration names a fixed worker ID from `core/paths.py`, supplies user-visible text, and may declare worker-specific compatibility details:

```python
operation(
    "compact",
    "linux-compact",
    pass_filesystem=True,
    warning="...",
)
```

Capability bits and operation declarations must agree exactly. Plugin construction fails immediately when a backend advertises a mutation bit without a matching operation, declares an operation twice, or uses an invalid worker identifier.

The GUI does not select filesystem workers. It asks `operation_engine.py` to perform an operation for a plugin ID. The operation engine loads the registry, resolves the plugin's `OperationSpec`, removes options the worker explicitly does not support, and executes the fixed worker. The privileged helper only permits the central operation engine, the read-only mapper and `udisksctl unmount`. FAT analysis is reached through the FAT plugin like every other filesystem.

Current worker IDs are:

- `fat-native` — native C FAT12/16/32 engine.
- `exfat` — native exFAT worker.
- `ntfs` — native NTFS worker.
- `affs` — Amiga OFS/FFS worker.
- `apple` — HFS/HFS+/HFSX worker.
- `linux-compact` — ext4, Btrfs and XFS compact worker.

Adding a filesystem with an existing worker therefore requires only a backend module and one registry entry. Adding a new mutation implementation also requires one fixed path entry and the worker itself; no filesystem-specific GUI or root-helper branch is required.

## Map schema

`allocation_mapper.py --list-backends` returns schema 2 and includes each plugin's operations. `allocation_mapper.py --probe` returns the selected plugin and the same operation manifest.

Each map cell contains allocation-unit counts for:

- `free`
- `used`
- `unknown`
- `bad`
- `fragmented`
- `directory`

The ranges are half-open internally. Shared aggregation rejects overlapping state ranges and treats uncovered spans as unknown. `aggregate_ranges()` runs in `O(cells + ranges)` after sorting/normalisation, avoiding the old per-cell scan across every filesystem extent.

## Exact native backends

Btrfs reports `exact-single-device` because logical-to-physical translation is exact only for supported single-device, non-striped layouts. Unsupported multi-device or striped profiles fail explicitly.

XFS reports `exact` after walking each allocation group's block-number free-space B+tree and taking its complement. Fragmentation comes from decoded inode data-fork extents, including external bmap B+trees.

EXT filesystems report exact allocation from block-group metadata and inode mappings. NTFS reports exact allocation from `$Bitmap` and overlays supported physical stream mappings.
