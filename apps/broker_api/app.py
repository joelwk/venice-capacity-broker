from __future__ import annotations

"""
Thin compatibility shim so `from apps.broker_api.app import app` works.

The real implementation lives in `apps/broker-api/app.py` (hyphenated dir),
which is not importable as a Python package name. We load it via importlib
and re-export the FastAPI `app` instance.
"""

import sys
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path


def _load_impl_module():
    # Locate the real file: apps/broker-api/app.py
    here = Path(__file__).resolve()
    impl_path = here.parent.parent / "broker-api" / "app.py"
    if not impl_path.exists():
        raise ModuleNotFoundError(f"Implementation not found at {impl_path}")

    spec = spec_from_file_location("apps_broker_api_impl", str(impl_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to create module spec for {impl_path}")

    mod = module_from_spec(spec)
    # Pre-register module to handle potential relative imports during exec
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


# Load once at import time
_impl = _load_impl_module()

# Re-export FastAPI `app`
try:
    app = getattr(_impl, "app")
except AttributeError as e:
    raise AttributeError("`app` not found in loaded implementation module") from e

