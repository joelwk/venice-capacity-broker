"""
Venice Capacity Broker API package.

This module exposes helper entrypoints while keeping the FastAPI app defined
in ``apps.broker_api.app``.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing aid only
    from fastapi import FastAPI

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any):
    """Proxy to ``apps.broker_api.app.create_app``."""
    module = import_module(".app", __name__)
    return module.create_app(*args, **kwargs)
