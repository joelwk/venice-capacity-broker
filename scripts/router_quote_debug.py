from __future__ import annotations

import argparse
import os
from pathlib import Path

from libs.dex.routes import RoutePlan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_env() -> None:
    try:
        from libs.env import load_dotenv_if_present  # type: ignore

        load_dotenv_if_present(path=str(_repo_root() / ".env"), override=False)
    except Exception:
        pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect router quotes for configured trade path")
    parser.add_argument("--mode", choices=("v2", "v3"), default="v2", help="router flavor to query")
    parser.add_argument("--amount", type=int, help="amount-in in base units (defaults to 1 token)")
    parser.add_argument("--path", type=str, help="override route spec (defaults to TRADE_PATH)")
    parser.add_argument("--exact-out", dest="exact_out", type=int, help="quote exact-out requirement")
    return parser.parse_args()


def _default_amount() -> int:
    try:
        dec = int(os.getenv("DIEM_DECIMALS") or 18)
    except Exception:
        dec = 18
    try:
        return int(os.getenv("QUOTE_DEBUG_AMOUNT") or (10 ** dec))
    except Exception:
        return 10 ** dec


def _ensure_route(spec: str) -> RoutePlan:
    from services.marketdata.provider import MarketDataProvider

    md = MarketDataProvider()
    return md._parse_route_spec(spec)  # type: ignore[attr-defined]


def main() -> None:
    _load_env()
    args = _parse_args()
    from libs.agentkit_ext.web3_utils import get_web3, get_contract
    from web3 import Web3  # type: ignore

    route_spec = args.path or os.getenv("TRADE_PATH")
    if not route_spec:
        print("TRADE_PATH is not set")
        raise SystemExit(2)
    try:
        route = _ensure_route(route_spec)
    except Exception as exc:  # noqa: BLE001
        print(f"invalid route spec: {exc}")
        raise SystemExit(2) from exc

    amount = args.amount or _default_amount()
    rpc = os.getenv("RPC_URL") or os.getenv("BASE_RPC_URL")
    chain_id = os.getenv("BASE_CHAIN_ID")
    print(f"RPC_URL={rpc} chain_id={chain_id}")
    print(f"route.tokens={route.tokens}")
    print(f"amount_in={amount}")

    w3 = get_web3()
    print(f"connected={w3.is_connected()} block={w3.eth.block_number}")

    if args.mode == "v2":
        router = os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv("ROUTER_ADDRESS")
        if not router:
            print("UNISWAP_V2_ROUTER_ADDRESS is not set")
            raise SystemExit(2)
        path = route.to_uniswap_v2_path(checksum=True)
        r = get_contract(w3, Web3.to_checksum_address(router), "uniswap_v2_router.json")
        try:
            amts = r.functions.getAmountsOut(amount, path).call()
            print(f"getAmountsOut -> {amts}")
        except Exception as exc:  # noqa: BLE001
            print(f"getAmountsOut error: {exc}")
            try:
                amt2 = max(1, amount // 10**6)
                amts2 = r.functions.getAmountsOut(amt2, path).call()
                print(f"getAmountsOut (amt/1e6) -> {amts2}")
            except Exception as exc2:  # noqa: BLE001
                print(f"getAmountsOut (amt/1e6) error: {exc2}")
        return

    router = os.getenv("UNISWAP_V3_ROUTER_ADDRESS")
    quoter = os.getenv("UNISWAP_V3_QUOTER_ADDRESS")
    if not router or not quoter:
        print("UNISWAP_V3_ROUTER_ADDRESS or UNISWAP_V3_QUOTER_ADDRESS not set")
        raise SystemExit(2)

    provider_default_fee = os.getenv("UNISWAP_V3_DEFAULT_FEE")
    filled_route = route
    if any(h.fee is None for h in route.hops):
        if provider_default_fee is None:
            raise SystemExit("route is missing fee tiers and UNISWAP_V3_DEFAULT_FEE is unset")
        filled_route = route.with_default_fee(int(provider_default_fee))

    quoter_contract = get_contract(w3, Web3.to_checksum_address(quoter), "uniswap_v3_quoter.json")
    try:
        out_data = quoter_contract.functions.quoteExactInput(filled_route.to_uniswap_v3_path_bytes(), amount).call()
        amount_out = out_data[0] if isinstance(out_data, (list, tuple)) else out_data
        print(f"quoteExactInput -> amount_out={amount_out} raw={out_data}")
    except Exception as exc:  # noqa: BLE001
        print(f"quoteExactInput error: {exc}")

    if args.exact_out:
        try:
            in_data = quoter_contract.functions.quoteExactOutput(
                filled_route.to_uniswap_v3_path_bytes(reverse=True),
                int(args.exact_out),
            ).call()
            amount_in = in_data[0] if isinstance(in_data, (list, tuple)) else in_data
            print(f"quoteExactOutput -> amount_in={amount_in} raw={in_data}")
        except Exception as exc:  # noqa: BLE001
            print(f"quoteExactOutput error: {exc}")


if __name__ == "__main__":
    main()
