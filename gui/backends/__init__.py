"""Filesystem plugin package for Linux Defragger."""

from .base import BackendError, FilesystemBackend
from .registry import Registry

__all__ = ["BackendError", "FilesystemBackend", "Registry"]
