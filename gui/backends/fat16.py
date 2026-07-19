# Linux Defragger
# Author: Shannon Smith
# Purpose: Modular filesystem analysis, compaction and defragmentation support.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""FAT16 backend declaration using the shared FAT implementation."""

from .fat_common import FatBackend
BACKEND = FatBackend(16)
