"""Runtime tweaks for local development/test environments."""

from __future__ import annotations

import os
import sys

_SYSTEM_DYNLOAD = "/usr/lib/python3.12/lib-dynload"

if _SYSTEM_DYNLOAD not in sys.path and os.path.isdir(_SYSTEM_DYNLOAD):
    sys.path.append(_SYSTEM_DYNLOAD)
