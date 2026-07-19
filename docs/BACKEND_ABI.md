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
Only FAT currently advertises `CAP_GROWTH_DEFRAG`; this capability means the
backend can deliberately rebuild a proportional post-file growth layout.
The NTFS module routes Compact and Defragment to separate planners in the native
NTFS engine, with a shared external recovery-journal contract.

Map results use a common JSON schema.  Each cell reports `free`, `used`,
`unknown`, `bad`, `fragmented`, and `directory` allocation-unit counts.  A
backend must mark unavailable positional knowledge as `unknown`; it must never
invent a physical layout.
