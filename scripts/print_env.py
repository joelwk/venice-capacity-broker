from __future__ import annotations

import json
import os
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_env() -> None:
    # Prefer the project helper so behavior matches CLI/Broker
    try:
        from libs.env import load_dotenv_if_present  # type: ignore

        load_dotenv_if_present(path=str(_repo_root() / ".env"), override=False)
        return
    except Exception:
        pass
    # Fallback to python-dotenv if available
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=str(_repo_root() / ".env"), override=False)
    except Exception:
        pass


def _mask(val: str | None, keep: int = 4) -> str | None:
    if not val:
        return None
    s = str(val)
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "…" + s[-keep:]


def main() -> None:
    _load_env()
    abi_dir = _repo_root() / "abi"
    def has_abi(name: str) -> bool:
        try:
            return (abi_dir / name).exists()
        except Exception:
            return False

    out = {
        "web3": {
            "RPC_URL": os.getenv("RPC_URL"),
            "BASE_RPC_URL": os.getenv("BASE_RPC_URL"),
            "BASE_CHAIN_ID": os.getenv("BASE_CHAIN_ID"),
        },
        "dex": {
            "DEX_PROVIDERS": os.getenv("DEX_PROVIDERS"),
            "UNISWAP_V2_ROUTER_ADDRESS": os.getenv("UNISWAP_V2_ROUTER_ADDRESS"),
            "AERODROME_ROUTER_ADDRESS": os.getenv("AERODROME_ROUTER_ADDRESS"),
            "AERODROME_STABLE": os.getenv("AERODROME_STABLE"),
            "UNISWAP_V2_FACTORY_ADDRESS": os.getenv("UNISWAP_V2_FACTORY_ADDRESS"),
            "AERODROME_FACTORY_VOLATILE": os.getenv("AERODROME_FACTORY_VOLATILE"),
            "AERODROME_FACTORY_STABLE": os.getenv("AERODROME_FACTORY_STABLE"),
        },
        "tokens": {
            "QUOTE_TOKEN_ADDRESS": os.getenv("QUOTE_TOKEN_ADDRESS"),
            "DIEM_TOKEN_ADDRESS": os.getenv("DIEM_TOKEN_ADDRESS"),
            "VVV_TOKEN_ADDRESS": os.getenv("VVV_TOKEN_ADDRESS"),
            "WETH_ADDRESS": os.getenv("WETH_ADDRESS"),
            "BRIDGE_TOKEN_ADDRESS": os.getenv("BRIDGE_TOKEN_ADDRESS"),
        },
        "pricing": {
            "TRADE_PATH": os.getenv("TRADE_PATH"),
            "RISK_MAX_SLIPPAGE_BPS": os.getenv("RISK_MAX_SLIPPAGE_BPS"),
            "RISK_MAX_POOL_TAKE_BPS": os.getenv("RISK_MAX_POOL_TAKE_BPS") or os.getenv("RISK_MAX_POOL_TAKE_BP"),
        },
        "venice": {
            "VENICE_API_BASE_URL": os.getenv("VENICE_API_BASE_URL"),
            "VENICE_API_KEY": _mask(os.getenv("VENICE_API_KEY")),
        },
        "abi": {
            "erc20.json": has_abi("erc20.json"),
            "uniswap_v2_router.json": has_abi("uniswap_v2_router.json"),
            "aerodrome_router.json": has_abi("aerodrome_router.json"),
            "diem.json": has_abi("diem.json"),
        },
        "notes": {
            "loaded_dotenv": ( _repo_root() / ".env" ).exists(),
            "cwd": str(Path.cwd()),
            "repo_root": str(_repo_root()),
        },
    }

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

