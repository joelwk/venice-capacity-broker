from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path

_BOOTSTRAPPED = False
_TRUTHY = {"1", "true", "yes", "on"}


def _is_test_context() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return True
    testing = (os.getenv("TESTING") or "").strip().lower()
    if testing in _TRUTHY:
        return True
    ci = (os.getenv("CI") or "").strip().lower()
    if ci in _TRUTHY:
        return True
    return False


def load_dotenv_if_present(
    path: str | None = None,
    override: bool = False,
    *,
    allow_in_tests: bool = True,
) -> bool:
    """Load a .env file into os.environ if present.

    - Uses python-dotenv when installed.
    - Falls back to a tiny parser supporting KEY=VALUE and `export KEY=VALUE`.
    - Ignores comments and blank lines.
    - Returns True if a file was found and parsed (even if no vars changed).
    - Does not override existing vars unless `override=True`.
    """
    if os.getenv("DISABLE_DOTENV"):
        return False
    if not allow_in_tests and (
        os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules
    ):
        return False

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
                    line = line[len("export ") :]
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip optional surrounding quotes
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                if key and (override or key not in os.environ):
                    os.environ[key] = val
        return True
    except Exception:
        # Never crash; just act as if file not loaded
        return False


# --- Environment helpers ---


def get_app_env() -> str:
    raw_env = os.getenv("APP_ENV") or os.getenv("ENV") or os.getenv("ENVIRONMENT")
    if raw_env is not None and str(raw_env).strip() != "":
        env = str(raw_env).strip().lower()
        if env in {"production", "prod"}:
            return "production"
        if env in {"staging", "stage"}:
            return "staging"
        if env in {"test", "testing", "ci"}:
            return "test"
        if env == "development":
            return "development"
        return env
    if _is_test_context():
        return "test"
    return "development"


def is_production() -> bool:
    return get_app_env() == "production"


def is_test_env() -> bool:
    return get_app_env() == "test"


def env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _extract_env_defaults(text: str) -> dict[str, str]:
    defaults: dict[str, str] = {}
    idx = 0
    length = len(text)
    while True:
        start = text.find("${", idx)
        if start < 0:
            break
        cursor = start + 2
        var_chars: list[str] = []
        while cursor < length and text[cursor] not in {":", "}"}:
            var_chars.append(text[cursor])
            cursor += 1
        if cursor >= length:
            break
        var_name = "".join(var_chars).strip()
        if not var_name:
            idx = start + 2
            continue
        if text[cursor] == "}":
            idx = cursor + 1
            continue
        cursor += 1
        default_chars: list[str] = []
        nested = 0
        end = None
        while cursor < length:
            ch = text[cursor]
            if ch == "{":
                nested += 1
            elif ch == "}":
                if nested == 0:
                    end = cursor
                    break
                nested -= 1
            default_chars.append(ch)
            cursor += 1
        if end is None:
            break
        default_val = "".join(default_chars).strip()
        if default_val != "":
            defaults[var_name] = default_val
        idx = end + 1
    return defaults


def _read_default_yaml(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    return _extract_env_defaults(text)


def _read_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values  # type: ignore

        values = dotenv_values(dotenv_path=str(path))
        parsed: dict[str, str] = {}
        for key, value in values.items():
            if key and value is not None:
                parsed[str(key)] = str(value)
        return parsed
    except Exception:
        pass

    parsed: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip().rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :]
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                if key:
                    parsed[key] = val
    except Exception:
        return {}
    return parsed


def _apply_env_map(values: dict[str, str], locked: Iterable[str]) -> tuple[int, int]:
    locked_set = set(locked)
    applied = 0
    skipped = 0
    for key, value in values.items():
        if key in locked_set:
            skipped += 1
            continue
        os.environ[key] = value
        applied += 1
    return applied, skipped


def _detect_runtime_mode() -> str:
    if os.getenv("REPL_ID") or os.getenv("REPLIT_DB_URL") or os.getenv("REPL_SLUG"):
        return "replit"
    if Path("/.dockerenv").exists():
        return "docker"
    return "local"


def bootstrap_env(
    *,
    repo_root: Path | None = None,
    mode: str | None = None,
    reload: bool = False,
    enable_dotenv: bool = True,
    enable_defaults: bool = True,
) -> dict[str, list[str]]:
    """Load config/default.yml defaults plus dotenv files in a stable order."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED and not reload:
        return {"loaded": []}
    test_context = _is_test_context()
    if test_context and not env_flag("ALLOW_DEFAULTS_IN_TESTS", False):
        enable_defaults = False
    if test_context and not env_flag("ALLOW_DOTENV_IN_TESTS", False):
        enable_dotenv = False
    root = repo_root or Path(__file__).resolve().parents[1]
    runtime_mode = (mode or _detect_runtime_mode()).strip().lower()
    loaded: list[str] = []
    # Only lock non-empty values so defaults can fill blank envs set by CI/hosting.
    locked_keys = {key for key, value in os.environ.items() if str(value).strip()}

    if enable_defaults:
        default_path = root / "config" / "default.yml"
        if not default_path.exists():
            default_path = root / "config" / "default.yaml"
        if default_path.exists():
            defaults = _read_default_yaml(default_path)
            if defaults:
                _apply_env_map(defaults, locked_keys)
                loaded.append(str(default_path))

    if enable_dotenv and not os.getenv("DISABLE_DOTENV"):
        dotenv_paths: list[Path] = []
        if runtime_mode == "docker" and not os.getenv("LOAD_DOTENV_IN_DOCKER"):
            dotenv_paths = []
        elif runtime_mode == "replit":
            dotenv_paths = [root / ".env"]
        else:
            dotenv_paths = [root / ".env", root / "docker" / ".env.local"]
        for path in dotenv_paths:
            if not path.exists():
                continue
            values = _read_dotenv_file(path)
            if not values:
                continue
            _apply_env_map(values, locked_keys)
            loaded.append(str(path))

    if _is_test_context():
        os.environ.setdefault("ALLOW_SQLITE_FALLBACK", "1")
        os.environ.setdefault("ALLOW_JSON_FALLBACK", "1")
        os.environ.setdefault("DRY_RUN", "1")

    _BOOTSTRAPPED = True
    return {"loaded": loaded}


__all__ = [
    "bootstrap_env",
    "env_flag",
    "get_app_env",
    "is_production",
    "is_test_env",
    "load_dotenv_if_present",
]
