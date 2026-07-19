#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Standard mutation dispatcher for all filesystem plugins.

"""Resolve a filesystem plugin and execute its declared mutation worker.

The GUI calls this one engine for every mutation.  Filesystem-specific worker
selection and option compatibility are properties of the plugin manifest, not
GUI conditionals or privileged-helper policy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backends import BackendError, Registry
from core.paths import resolve_program
from version import VERSION


def _without_options(arguments: list[str], unsupported: tuple[str, ...]) -> list[str]:
    """Remove unsupported ``--option value`` pairs before worker execution."""

    if not unsupported:
        return arguments
    blocked = set(unsupported)
    filtered: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in blocked:
            index += 1
            if index < len(arguments) and not arguments[index].startswith("--"):
                index += 1
            continue
        if any(token.startswith(option + "=") for option in blocked):
            index += 1
            continue
        filtered.append(token)
        index += 1
    return filtered


def build_worker_command(
    registry: Registry,
    filesystem: str,
    operation_name: str,
    device: str,
    forwarded: list[str],
) -> list[str]:
    backend = registry.by_fstype(filesystem)
    if backend is None:
        raise BackendError(f"no filesystem plugin is registered for {filesystem!r}")
    spec = backend.info.operation(operation_name)
    if spec is None:
        raise BackendError(
            f"the {backend.info.display_name} plugin does not implement {operation_name}"
        )
    worker = resolve_program(spec.worker, anchor=HERE / "core")
    arguments = _without_options(forwarded, spec.unsupported_options)
    command = [worker, operation_name, device, *arguments]
    if spec.pass_filesystem:
        command.extend(["--filesystem", backend.info.id])
    return command


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Dispatch a standard Linux Defragger operation through a filesystem plugin"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--list-plugins", action="store_true")
    parser.add_argument("operation", nargs="?")
    parser.add_argument("device", nargs="?")
    parser.add_argument("--filesystem", default="")
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    args, forwarded = parse_args(sys.argv[1:] if argv is None else argv)
    registry = Registry()
    if args.list_plugins:
        print(json.dumps({"schema": 2, "backends": registry.manifest()}, separators=(",", ":")))
        return 0
    if not args.operation or not args.device or not args.filesystem:
        print("operation, device and --filesystem are required", file=sys.stderr)
        return 2
    try:
        command = build_worker_command(
            registry,
            args.filesystem,
            args.operation,
            args.device,
            forwarded,
        )
    except (BackendError, FileNotFoundError, ValueError) as exc:
        print(f"linux-defragger-operation-engine: {exc}", file=sys.stderr)
        return 2
    os.execv(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
