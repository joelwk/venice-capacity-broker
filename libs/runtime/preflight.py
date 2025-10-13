from __future__ import annotations

import importlib
import os
from typing import Iterable, Optional, Sequence


DEFAULT_INSTALL_HINT = "uv sync --extra broker --extra web3 --extra agentkit --extra graph --extra db"


def ensure_agentkit_installed(logger: Optional[object] = None, install_hint: Optional[str] = None) -> None:
    """Raise RuntimeError with guidance when coinbase-agentkit is missing."""

    try:
        importlib.import_module("coinbase_agentkit.wallet_providers")
    except ModuleNotFoundError as exc:  # noqa: PERF203
        hint = install_hint or DEFAULT_INSTALL_HINT
        msg = (
            "coinbase-agentkit is required for Base wallet operations. "
            f"Install the extras bundle via `{hint}` or `pip install '.[agentkit,broker,web3,graph,db]'`."
        )
        _emit(logger, msg, level="error")
        raise RuntimeError(msg) from exc


def validate_live_wallet_env(required: Sequence[str], logger: Optional[object] = None) -> list[str]:
    """Return environment variables that are missing for live wallet usage."""

    missing = [name for name in required if not _env_present(name)]
    if missing:
        msg = "Missing required environment variables: " + ", ".join(missing)
        _emit(logger, msg, level="error")
    return missing


def warn_optional_env(optional: Iterable[str], logger: Optional[object] = None) -> None:
    """Log a warning for optional environment gaps."""

    missing = [name for name in optional if not _env_present(name)]
    if missing:
        msg = "Optional environment variables not set: " + ", ".join(missing)
        _emit(logger, msg, level="warning")


def _emit(logger: Optional[object], message: str, level: str = "info") -> None:
    if logger is None:
        print(f"[preflight] {message}")
        return
    if hasattr(logger, level):
        getattr(logger, level)(message)
    else:
        logger.info(message)  # type: ignore[attr-defined]


def _env_present(name: str) -> bool:
    raw = os.getenv(name)
    return bool(raw and str(raw).strip())
