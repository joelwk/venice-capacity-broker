#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from libs.env import load_dotenv_if_present  # type: ignore
except Exception as exc:  # pragma: no cover - fallback should be rare
    raise RuntimeError(
        f"failed to import libs.env (required for env export): {exc}"
    ) from exc


def _iter_keys(path: Path) -> Iterable[str]:
    if not path.exists():
        return ()
    keys: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if not line or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key:
                keys.add(key)
    return keys


def main() -> None:
    env_files = [
        REPO_ROOT / ".env",
        REPO_ROOT / ".env.docker",
        REPO_ROOT / "docker" / ".env.local",
    ]

    keys: set[str] = set()
    for file_path in env_files:
        keys.update(_iter_keys(file_path))

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    docker_env = REPO_ROOT / ".env.docker"
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
    local_env = REPO_ROOT / "docker" / ".env.local"
    if local_env.exists():
        load_dotenv_if_present(path=str(local_env), override=True)

    exports = []
    for key in sorted(keys):
        value = os.getenv(key)
        if value is None:
            continue
        exports.append(f"export {key}={shlex.quote(value)}")

    if exports:
        print("; ".join(exports))


if __name__ == "__main__":
    main()
