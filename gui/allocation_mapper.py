#!/usr/bin/python3
"""Self-contained modular read-only filesystem allocation mapper."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from backends import Registry, BackendError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?")
    parser.add_argument("--fstype", default="")
    parser.add_argument("--cells", type=int, default=4096)
    parser.add_argument("--list-backends", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    registry = Registry()
    if args.list_backends:
        print(json.dumps({"schema": 1, "backends": registry.manifest()}, separators=(",", ":")))
        return 0
    if not args.path:
        parser.error("path is required")
    backend = registry.by_fstype(args.fstype) if args.fstype else registry.probe(args.path)
    if backend is None:
        print("No self-contained filesystem backend recognised this volume.", file=sys.stderr)
        return 2
    if args.probe:
        print(json.dumps({"filesystem": backend.info.id, "capabilities": backend.info.capabilities,
                          "map_accuracy": backend.info.map_accuracy}))
        return 0
    if backend.info.id.startswith("fat"):
        print("FAT volumes are mapped by the native FAT engine.", file=sys.stderr)
        return 2
    try:
        result = backend.map(args.path, max(1, args.cells))
    except (OSError, BackendError, ValueError) as exc:
        print(f"{backend.info.display_name} mapper: {exc}", file=sys.stderr)
        return 1
    result["capabilities"] = backend.info.capabilities
    result["backend_id"] = backend.info.id
    print(json.dumps(result, separators=(",", ":")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
