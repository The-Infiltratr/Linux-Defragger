#!/usr/bin/python3
# Linux Defragger
# Author: Shannon Smith
# Purpose: Stable compatibility surface for filesystem plugins.

"""Public filesystem-plugin ABI.

Contracts, raw I/O and range aggregation live in focused modules.  Existing
plugins import from ``backends.base`` so the public ABI remains stable while the
implementation stays modular.
"""

from .contracts import *  # noqa: F401,F403
from .io import *  # noqa: F401,F403
from .ranges import *  # noqa: F401,F403
