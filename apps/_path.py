"""Utilities to ensure repo root is importable when running modules directly."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def add_repo_root_to_sys_path() -> None:
    """Prepend the repository root to sys.path once."""
    root_str = str(REPO_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


add_repo_root_to_sys_path()

__all__ = ["REPO_ROOT", "add_repo_root_to_sys_path"]
