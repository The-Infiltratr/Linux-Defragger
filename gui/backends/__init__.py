# Linux Defragger
# Author: Shannon Smith
# Purpose: Filesystem backend package.
#
# Comments describe design intent and non-obvious behaviour. They are kept
# concise so that the implementation remains readable and maintainable.

"""Filesystem backend package for Linux Defragger."""

from .registry import Registry
from .base import *
