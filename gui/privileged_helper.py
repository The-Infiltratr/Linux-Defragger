#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Persistent privileged helper for the Linux Defragger GTK application.

The helper is started once through pkexec and remains attached to the GUI over
stdin/stdout. It accepts only a small fixed command set, streams child output
as JSON messages, and can deliver SIGINT to the active engine process group.
One authenticated helper is retained for the lifetime of the GUI session.
"""

from __future__ import annotations

import json
import os
import re
import time
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
ENGINE = Path("/usr/bin/linux-defragger-engine")
MAPPER = Path("/usr/lib/linux-defragger/allocation_mapper.py")
EXFAT_ENGINE = Path("/usr/lib/linux-defragger/exfat_engine.py")
AFFS_ENGINE = Path("/usr/lib/linux-defragger/affs_engine.py")
APPLE_ENGINE = Path("/usr/lib/linux-defragger/apple_engine.py")
NTFS_ENGINE = Path("/usr/lib/linux-defragger/ntfs_engine.py")
UDISKSCTL = Path("/usr/bin/udisksctl")

_emit_lock = threading.Lock()
_active_lock = threading.Lock()
_active_process: subprocess.Popen[str] | None = None
_active_request_id: int | None = None
_shutdown_requested = False
_active_has_output = False
_pending_stop = False
_NTFS_PROGRESS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+percent completed\s*$", re.IGNORECASE)


def emit(message: dict[str, Any]) -> None:
    encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    with _emit_lock:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


def fail(request_id: int | None, message: str) -> None:
    emit({"type": "error", "id": request_id, "message": message})


def allowed_command(program: str, argv: list[str]) -> list[str]:
    if program == "engine":
        if not ENGINE.is_file() or not os.access(ENGINE, os.X_OK):
            raise RuntimeError(f"engine is unavailable: {ENGINE}")
        if not argv or argv[0] not in {"analyze", "map", "defrag", "compact", "recover"}:
            raise RuntimeError("engine command is not allowed")
        return [str(ENGINE), *argv]
    if program == "exfat-engine":
        if not EXFAT_ENGINE.is_file() or not os.access(EXFAT_ENGINE, os.X_OK):
            raise RuntimeError(f"exFAT engine is unavailable: {EXFAT_ENGINE}")
        if not argv or argv[0] not in {"defrag", "compact", "recover"}:
            raise RuntimeError("exFAT engine command is not allowed")
        return [str(EXFAT_ENGINE), *argv]
    if program == "affs-engine":
        if not AFFS_ENGINE.is_file() or not os.access(AFFS_ENGINE, os.X_OK):
            raise RuntimeError(f"Amiga filesystem engine is unavailable: {AFFS_ENGINE}")
        if not argv or argv[0] not in {"defrag", "compact", "recover"}:
            raise RuntimeError("Amiga filesystem engine command is not allowed")
        return [str(AFFS_ENGINE), *argv]
    if program == "apple-engine":
        if not APPLE_ENGINE.is_file() or not os.access(APPLE_ENGINE, os.X_OK):
            raise RuntimeError(f"Apple filesystem engine is unavailable: {APPLE_ENGINE}")
        if not argv or argv[0] not in {"defrag", "compact", "recover"}:
            raise RuntimeError("Apple filesystem engine command is not allowed")
        return [str(APPLE_ENGINE), *argv]
    if program == "ntfs-engine":
        if not NTFS_ENGINE.is_file() or not os.access(NTFS_ENGINE, os.X_OK):
            raise RuntimeError(f"NTFS engine is unavailable: {NTFS_ENGINE}")
        if not argv or argv[0] not in {"compact", "recover"}:
            raise RuntimeError("NTFS engine command is not allowed")
        return [str(NTFS_ENGINE), *argv]
    if program == "mapper":
        if not MAPPER.is_file() or not os.access(MAPPER, os.X_OK):
            raise RuntimeError(f"allocation mapper is unavailable: {MAPPER}")
        return [str(MAPPER), *argv]
    if program == "udisksctl":
        if not UDISKSCTL.is_file() or not os.access(UDISKSCTL, os.X_OK):
            raise RuntimeError(f"udisksctl is unavailable: {UDISKSCTL}")
        if len(argv) != 3 or argv[0] != "unmount" or argv[1] != "-b" or not argv[2].startswith("/dev/"):
            raise RuntimeError("only udisksctl unmount -b /dev/... is allowed")
        return [str(UDISKSCTL), *argv]
    raise RuntimeError("unknown helper program")


def run_request(request_id: int, program: str, argv: list[str]) -> None:
    global _active_process, _active_request_id, _active_has_output, _pending_stop
    try:
        command = allowed_command(program, argv)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        with _active_lock:
            _active_process = process
            _active_request_id = request_id
            _active_has_output = False
            _pending_stop = False
        emit({"type": "started", "id": request_id, "pid": process.pid, "pgid": os.getpgid(process.pid)})
        assert process.stdout is not None
        last_progress: float | None = None
        last_progress_emit = 0.0
        for line in process.stdout:
            deliver_queued_stop = False
            with _active_lock:
                if not _active_has_output:
                    _active_has_output = True
                    deliver_queued_stop = _pending_stop
                    _pending_stop = False
            clean_line = line.rstrip("\r\n")
            progress_match = _NTFS_PROGRESS_RE.match(clean_line)
            if progress_match is not None:
                percent = max(0.0, min(100.0, float(progress_match.group(1))))
                now = time.monotonic()
                if (last_progress is None or abs(percent - last_progress) >= 0.05 or
                        now - last_progress_emit >= 0.25 or percent >= 100.0):
                    emit({"type": "progress", "id": request_id, "percent": percent})
                    last_progress = percent
                    last_progress_emit = now
            else:
                emit({"type": "output", "id": request_id, "line": clean_line})
            if deliver_queued_stop:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGINT)
                    emit({"type": "stop-result", "id": None, "active_id": request_id, "delivered": True, "message": "queued SIGINT delivered after engine initialisation"})
                except Exception as exc:
                    emit({"type": "stop-result", "id": None, "active_id": request_id, "delivered": False, "message": str(exc)})
        returncode = process.wait()
        emit({"type": "finished", "id": request_id, "returncode": returncode})
    except Exception as exc:
        # Never report failure and abandon a root-owned writer.  If the helper
        # itself encounters an unexpected transport/decoding error, request
        # the engine's journal-safe SIGINT path and wait for it to leave the
        # block device before notifying the GUI.
        process = locals().get("process")
        if isinstance(process, subprocess.Popen) and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
                process.wait(timeout=120)
            except Exception as stop_exc:
                fail(request_id, f"helper error: {exc}; safe-stop error: {stop_exc}")
            else:
                fail(request_id, f"helper error; engine stopped safely: {exc}")
        else:
            fail(request_id, str(exc))
        emit({"type": "finished", "id": request_id, "returncode": 127})
    finally:
        with _active_lock:
            _active_process = None
            _active_request_id = None
            _active_has_output = False
            _pending_stop = False


def handle_run(message: dict[str, Any]) -> None:
    request_id = int(message.get("id", 0))
    program = str(message.get("program", ""))
    argv_raw = message.get("argv")
    if not isinstance(argv_raw, list) or not all(isinstance(x, str) for x in argv_raw):
        fail(request_id, "argv must be a list of strings")
        return
    with _active_lock:
        if _active_process is not None:
            fail(request_id, "another privileged operation is already active")
            return
    threading.Thread(target=run_request, args=(request_id, program, list(argv_raw)), daemon=True).start()


def handle_stop(message: dict[str, Any]) -> None:
    global _pending_stop
    request_id = message.get("id")
    with _active_lock:
        process = _active_process
        active_id = _active_request_id
        has_output = _active_has_output
        if process is not None and not has_output:
            _pending_stop = True
    if process is None:
        emit({"type": "stop-result", "id": request_id, "active_id": active_id, "delivered": False, "message": "no active operation"})
        return
    if not has_output:
        emit({"type": "stop-result", "id": request_id, "active_id": active_id, "delivered": True, "message": "safe stop queued until engine initialisation"})
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGINT)
        emit({"type": "stop-result", "id": request_id, "active_id": active_id, "delivered": True, "message": "SIGINT delivered"})
    except ProcessLookupError:
        emit({"type": "stop-result", "id": request_id, "active_id": active_id, "delivered": False, "message": "operation already exited"})
    except Exception as exc:
        emit({"type": "stop-result", "id": request_id, "active_id": active_id, "delivered": False, "message": str(exc)})


def main() -> int:
    if os.geteuid() != 0:
        print("Linux Defragger privileged helper must run as root", file=sys.stderr)
        return 1
    emit({"type": "ready", "protocol": PROTOCOL_VERSION, "pid": os.getpid()})
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            action = message.get("action")
            if action == "run":
                handle_run(message)
            elif action == "stop":
                handle_stop(message)
            elif action == "ping":
                emit({"type": "pong", "id": message.get("id")})
            elif action == "quit":
                with _active_lock:
                    process = _active_process
                if process is not None:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGINT)
                        process.wait(timeout=30)
                    except Exception:
                        pass
                emit({"type": "bye"})
                return 0
            else:
                fail(message.get("id"), "unknown helper action")
        except Exception as exc:
            fail(None, f"invalid request: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
