#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Read-only backend discovery and allocation-map dispatch.

"""Dispatch allocation-map requests through the filesystem plugin registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backends import BackendError, Registry  # noqa: E402
from version import VERSION  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Linux Defragger filesystem plugin dispatcher"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("path", nargs="?")
    parser.add_argument("--fstype", default="")
    parser.add_argument("--cells", type=int, default=4096)
    parser.add_argument("--list-backends", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    if not args.list_backends and not args.path:
        parser.error("path is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    registry = Registry()
    if args.list_backends:
        print(json.dumps({"schema": 2, "backends": registry.manifest()}, separators=(",", ":")))
        return 0

    assert args.path is not None
    backend = registry.by_fstype(args.fstype) if args.fstype else registry.probe(args.path)
    if backend is None:
        print("No self-contained filesystem plugin recognised this volume.", file=sys.stderr)
        return 2
    if args.probe:
        print(
            json.dumps(
                {
                    "filesystem": backend.info.id,
                    "capabilities": backend.info.capabilities,
                    "map_accuracy": backend.info.map_accuracy,
                    "operations": [item.manifest() for item in backend.info.operations],
                },
                separators=(",", ":"),
            )
        )
        return 0
    try:
        result = backend.map(args.path, max(1, args.cells))
    except (OSError, BackendError, FileNotFoundError, ValueError) as exc:
        print(f"{backend.info.display_name} mapper: {exc}", file=sys.stderr)
        return 1
    result["capabilities"] = backend.info.capabilities
    result["backend_id"] = backend.info.id
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
