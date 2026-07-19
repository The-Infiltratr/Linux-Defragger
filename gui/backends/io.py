#!/usr/bin/python3
"""Raw positional I/O and endian helpers shared by filesystem plugins."""

from __future__ import annotations

import fcntl
import os
import stat
import struct

from .contracts import BackendError

__all__ = ["Reader", "u16le", "u32le", "u64le", "u16be", "u32be", "u64be"]


class Reader:
    """Positional raw-device reader that retries until the requested span is complete."""

    _BLKGETSIZE64 = 0x80081272

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        info = os.fstat(self.fd)
        self.size = int(info.st_size)
        if stat.S_ISBLK(info.st_mode):
            try:
                raw = bytearray(8)
                fcntl.ioctl(self.fd, self._BLKGETSIZE64, raw, True)
                self.size = struct.unpack("=Q", raw)[0]
            except OSError:
                try:
                    self.size = os.lseek(self.fd, 0, os.SEEK_END)
                except OSError:
                    pass

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise BackendError("negative read")
        if length == 0:
            return b""
        chunks: list[bytes] = []
        remaining = length
        position = offset
        while remaining:
            chunk = os.pread(self.fd, remaining, position)
            if not chunk:
                received = length - remaining
                raise BackendError(
                    f"short read at byte {offset}: wanted {length}, got {received}"
                )
            chunks.append(chunk)
            position += len(chunk)
            remaining -= len(chunk)
        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def u16le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def u64le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def u16be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def u32be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def u64be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">Q", data, offset)[0]
