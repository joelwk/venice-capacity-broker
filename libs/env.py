from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv_if_present(path: Optional[str] = None, override: bool = False) -> bool:
    """Load a .env file into os.environ if present.

    - Uses python-dotenv when installed.
    - Falls back to a tiny parser supporting KEY=VALUE and `export KEY=VALUE`.
    - Ignores comments and blank lines.
    - Returns True if a file was found and parsed (even if no vars changed).
    - Does not override existing vars unless `override=True`.
    """
    dotenv_path = Path(path or ".env").resolve()

    # Prefer python-dotenv if available
    try:
        from dotenv import load_dotenv  # type: ignore

        return bool(load_dotenv(dotenv_path=str(dotenv_path), override=override))
    except Exception:
        pass

    if not dotenv_path.exists():
        return False

    try:
        with dotenv_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip().rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip optional surrounding quotes
                if (val.startswith("\"") and val.endswith("\"")) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key and (override or key not in os.environ):
                    os.environ[key] = val
        return True
    except Exception:
        # Never crash; just act as if file not loaded
        return False

