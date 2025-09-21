#!/usr/bin/env python
"""Utility to evaluate and optionally rotate DIEM trade paths.

- Loads environment variables from a provided .env file (default: repo `.env`).
- Evaluates both `TRADE_PATH` and `TRADE_PATH_2` (and the first two entries of `TRADE_PATHS` when present).
- If the primary path fails but the secondary succeeds, swaps them in the env file (unless `--dry-run`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

from libs.env import load_dotenv_if_present
from services.marketdata.provider import MarketDataProvider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rotate TRADE_PATH entries after verifying liquidity.")
    parser.add_argument("--env-file", default=".env", help="Path to the env file to update (default: .env)")
    parser.add_argument("--amount", type=float, default=1.0, help="Decimal amount used when quoting (default: 1.0)")
    parser.add_argument("--force", action="store_true", help="Swap regardless of primary health")
    parser.add_argument("--dry-run", action="store_true", help="Do not modify the env file")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating a .bak backup when writing")
    return parser.parse_args()


def _evaluate(provider: MarketDataProvider, spec: Optional[str], amount: float) -> Tuple[Optional[float], str]:
    if not spec:
        return None, "missing"
    try:
        route = provider._parse_route_spec(spec)
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid ({exc})"
    try:
        result = provider.best_price(route, amount_in_decimal=amount)
    except Exception as exc:  # noqa: BLE001
        return None, f"error ({exc})"
    price = float(result.get("price") or 0.0)
    if provider._valid_price(price):
        return price, "ok"
    return None, "zero"


def _read_env_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"env file {path} does not exist")
    return path.read_text().splitlines()


def _write_env_file(path: Path, lines: list[str], *, backup: bool) -> None:
    text = "\n".join(lines) + "\n"
    if backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text)


def _replace_line(lines: list[str], key: str, value: str) -> None:
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            return
    raise RuntimeError(f"{key} not found in env file")


def _swap_env(path: Path, primary: str, secondary: str, *, update_paths_json: Optional[str], backup: bool) -> None:
    lines = _read_env_file(path)
    _replace_line(lines, "TRADE_PATH", secondary)
    _replace_line(lines, "TRADE_PATH_2", primary)

    if update_paths_json:
        for idx, line in enumerate(lines):
            if line.startswith("TRADE_PATHS="):
                try:
                    arr = json.loads(line.split("=", 1)[1].strip())
                    if isinstance(arr, list) and len(arr) >= 2:
                        arr[0], arr[1] = arr[1], arr[0]
                        lines[idx] = f"TRADE_PATHS={json.dumps(arr)}"
                except Exception:
                    pass
                break

    _write_env_file(path, lines, backup=backup)


def main() -> int:
    args = _parse_args()
    env_path = Path(args.env_file)
    load_dotenv_if_present(path=str(env_path), override=True)

    primary_spec = os.getenv("TRADE_PATH")
    secondary_spec = os.getenv("TRADE_PATH_2")

    if not primary_spec and not secondary_spec:
        print("No TRADE_PATH variables found in environment.", file=sys.stderr)
        return 1

    md = MarketDataProvider()
    amount = float(args.amount)

    primary_price, primary_status = _evaluate(md, primary_spec, amount)
    secondary_price, secondary_status = _evaluate(md, secondary_spec, amount)

    print(f"Primary : {primary_spec or '<unset>'}\n  status : {primary_status}\n  price  : {primary_price}")
    print(f"Secondary : {secondary_spec or '<unset>'}\n  status   : {secondary_status}\n  price    : {secondary_price}")

    should_swap = False
    if args.force:
        should_swap = True
    elif primary_price is None and secondary_price is not None:
        should_swap = True

    if not should_swap:
        print("No swap required.")
        return 0

    if args.dry_run:
        print("Dry-run: skipping file update.")
        return 0

    update_paths_json = os.getenv("TRADE_PATHS")
    _swap_env(env_path, primary_spec or "", secondary_spec or "", update_paths_json=update_paths_json, backup=not args.no_backup)
    print(f"Swapped TRADE_PATH and TRADE_PATH_2 in {env_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
