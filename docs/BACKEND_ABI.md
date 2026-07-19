# Filesystem backend ABI — version 1

Backends are Python modules in `gui/backends`.  Each exports `BACKEND`, whose
`info` member is a `BackendInfo` record and which implements:

* `probe(path) -> bool`
* `map(path, cells) -> dict`

Capability bits are defined in `base.py`:

* `CAP_ANALYSE`
* `CAP_MAP`
* `CAP_COMPACT`
* `CAP_DEFRAG`
* `CAP_RECOVER`
* `CAP_LIVE_MAP`
* `CAP_GROWTH_DEFRAG`

The GUI obtains the backend manifest by executing:

```
allocation_mapper.py --list-backends
```

It builds the volume filter and enables controls from that manifest.  Adding a
read-only filesystem requires a new module and one registry entry; no GUI code
change is required. Mutation-capable modules advertise Compact, Defragment, Growth Defrag and
Recover independently; the GUI never infers one operation from another. The
three FAT modules share `fat_common.py` and the journalled native FAT engine.
FAT12/16/32 and exFAT advertise `CAP_GROWTH_DEFRAG`; this capability means the
backend can deliberately rebuild a proportional post-file growth layout.
The NTFS module routes Compact and Defragment to separate planners in the native
NTFS engine, with a shared external recovery-journal contract.

Map results use a common JSON schema.  Each cell reports `free`, `used`,
`unknown`, `bad`, `fragmented`, and `directory` allocation-unit counts.  A
backend must mark unavailable positional knowledge as `unknown`; it must never
invent a physical layout.


## Exact read-only tree-walking backends

A backend may report `map_accuracy` as `exact` when every data-device byte is
classified from native filesystem metadata. Btrfs uses
`exact-single-device` because its logical-to-physical chunk translation is exact
only for supported single-device, non-striped layouts; unsupported multi-device
or striped profiles must fail explicitly. XFS reports `exact` after walking each
allocation group's block-number free-space B+tree and taking its complement.

Fragmentation overlays must be derived from object allocation metadata, not from
visual adjacency in the map. Btrfs regular-file fragmentation comes from distinct
physical file extents. XFS file and directory fragmentation comes from decoded
data-fork extent records, including external bmap B+trees.
