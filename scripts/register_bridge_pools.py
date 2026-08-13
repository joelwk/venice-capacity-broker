#!/usr/bin/env python3
"""
Register the DIEM/VVV Aerodrome pair and the VVV/USDC Uniswap V3 pool with
their respective factories on Base.

In dry-run mode (default) the script reports the current registration status
without sending any transactions. Use ``--enable-live`` together with the
required confirmation flags to submit registration calls when a pool is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from web3 import Web3  # type: ignore
except Exception:  # pragma: no cover - optional in dry-run/unit-test environments
    from libs.dex.routes import _normalize_address

    class Web3:  # type: ignore
        @staticmethod
        def to_checksum_address(value: str) -> str:
            norm = _normalize_address(str(value))
            body = norm[2:] if norm.startswith("0x") else norm
            if len(body) < 40:
                body = body.zfill(40)
            elif len(body) > 40:
                body = body[-40:]
            return "0x" + body


from libs.agentkit_ext.web3_utils import (  # noqa: E402
    build_eip1559_tx,
    encode_contract_call,
    get_account_wallet,
    get_contract,
    get_web3,
    send_contract_tx,
)
from libs.env import load_dotenv_if_present  # noqa: E402

DEFAULT_ADDRESSES = {
    "DIEM_TOKEN_ADDRESS": "0xF4d97F2Da56e8c3098f3a8D538DB630A2606a024",
    "VVV_TOKEN_ADDRESS": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
    "QUOTE_TOKEN_ADDRESS": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "DIEM_VVV_PAIR_ADDRESS": "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d",
    "DIEM_VVV_FACTORY_ADDRESS": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
    "VVV_USDC_POOL_ADDRESS": "0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530",
    "VVV_USDC_POOL_FACTORY": "0x33128a8fC17869897dCe68Ed026d694621f6FdFd",
}

ALLOWED_SENDERS_ENV = "FACTORY_REGISTRATION_ALLOWED_ADDRESSES"
CONFIRM_MAINNET_ENV = "CONFIRM_MAINNET"

UNISWAP_V2_PAIR_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

UNISWAP_V3_POOL_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class BridgeAddresses:
    diem_token: str
    vvv_token: str
    quote_token: str
    diem_vvv_pair: str
    diem_vvv_factory: str
    diem_vvv_stable: bool
    vvv_usdc_pool: str
    vvv_usdc_factory: str
    vvv_usdc_fee: int


@dataclass
class RegistrationStatus:
    factory: str
    expected: str
    reported: str | None
    registered: bool
    notes: list[str]


def _normalize_address(raw: str, label: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    try:
        return Web3.to_checksum_address(value)
    except ValueError as exc:
        raise ValueError(f"{label} has invalid address value: {value}") from exc


def _env_address(key: str, label: str, *, default: str | None = None) -> str:
    raw = os.getenv(key, default or "")
    if not raw and default:
        raw = default
    return _normalize_address(raw, label)


def _env_bool(key: str, *, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, label: str, *, default: int | None = None) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        if default is None:
            raise ValueError(f"{label} is required")
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer, got {raw}") from exc
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


def load_addresses() -> BridgeAddresses:
    diem_vvv_stable_env = os.getenv("DIEM_VVV_STABLE")
    if diem_vvv_stable_env is not None:
        diem_vvv_stable = _env_bool("DIEM_VVV_STABLE")
    else:
        diem_vvv_stable = _env_bool("AERODROME_STABLE", default=False)
    return BridgeAddresses(
        diem_token=_env_address(
            "DIEM_TOKEN_ADDRESS",
            "DIEM_TOKEN_ADDRESS",
            default=DEFAULT_ADDRESSES["DIEM_TOKEN_ADDRESS"],
        ),
        vvv_token=_env_address(
            "VVV_TOKEN_ADDRESS",
            "VVV_TOKEN_ADDRESS",
            default=DEFAULT_ADDRESSES["VVV_TOKEN_ADDRESS"],
        ),
        quote_token=_env_address(
            "QUOTE_TOKEN_ADDRESS",
            "QUOTE_TOKEN_ADDRESS",
            default=DEFAULT_ADDRESSES["QUOTE_TOKEN_ADDRESS"],
        ),
        diem_vvv_pair=_env_address(
            "DIEM_VVV_PAIR_ADDRESS",
            "DIEM_VVV_PAIR_ADDRESS",
            default=DEFAULT_ADDRESSES["DIEM_VVV_PAIR_ADDRESS"],
        ),
        diem_vvv_factory=_env_address(
            "DIEM_VVV_FACTORY_ADDRESS",
            "DIEM_VVV_FACTORY_ADDRESS",
            default=DEFAULT_ADDRESSES["DIEM_VVV_FACTORY_ADDRESS"],
        ),
        diem_vvv_stable=diem_vvv_stable,
        vvv_usdc_pool=_env_address(
            "VVV_USDC_POOL_ADDRESS",
            "VVV_USDC_POOL_ADDRESS",
            default=DEFAULT_ADDRESSES["VVV_USDC_POOL_ADDRESS"],
        ),
        vvv_usdc_factory=_env_address(
            "VVV_USDC_POOL_FACTORY",
            "VVV_USDC_POOL_FACTORY",
            default=DEFAULT_ADDRESSES["VVV_USDC_POOL_FACTORY"],
        ),
        vvv_usdc_fee=_env_int("VVV_USDC_POOL_FEE", "VVV_USDC_POOL_FEE", default=3000),
    )


def _normalize_optional(addr: str) -> str | None:
    value = (addr or "").strip()
    if not value:
        return None
    try:
        checksum = Web3.to_checksum_address(value)
    except ValueError:
        return None
    if checksum.lower() == "0x" + "0" * 40:
        return None
    return checksum


def _describe_pair(w3, pair_addr: str) -> tuple[str | None, str | None, str | None]:
    contract = w3.eth.contract(address=pair_addr, abi=UNISWAP_V2_PAIR_ABI)
    try:
        token0 = contract.functions.token0().call()
    except Exception:
        token0 = None
    try:
        token1 = contract.functions.token1().call()
    except Exception:
        token1 = None
    try:
        factory = contract.functions.factory().call()
    except Exception:
        factory = None
    return (
        _normalize_optional(token0 or ""),
        _normalize_optional(token1 or ""),
        _normalize_optional(factory or ""),
    )


def _describe_pool(
    w3, pool_addr: str
) -> tuple[str | None, str | None, str | None, int | None]:
    contract = w3.eth.contract(address=pool_addr, abi=UNISWAP_V3_POOL_ABI)
    try:
        token0 = contract.functions.token0().call()
    except Exception:
        token0 = None
    try:
        token1 = contract.functions.token1().call()
    except Exception:
        token1 = None
    try:
        factory = contract.functions.factory().call()
    except Exception:
        factory = None
    try:
        fee = contract.functions.fee().call()
    except Exception:
        fee = None
    return (
        _normalize_optional(token0 or ""),
        _normalize_optional(token1 or ""),
        _normalize_optional(factory or ""),
        fee,
    )


def check_aerodrome_registration(w3, addresses: BridgeAddresses) -> RegistrationStatus:
    notes: list[str] = []
    reported: str | None = None
    # First, get the actual token order from the pair itself
    token0, token1, pair_factory = _describe_pair(w3, addresses.diem_vvv_pair)
    if token0 and token1:
        notes.append(f"Pair tokens: token0={token0}, token1={token1}")
        # Use the pair's actual token order, but also try sorted order
        token_orders_to_try = [
            (token0, token1),  # Pair's actual order
            _ordered_tokens(addresses.diem_token, addresses.vvv_token),  # Sorted order
        ]
    else:
        notes.append("Unable to read token0/token1 from pair (pair may not exist)")
        # Fall back to sorted order if we can't read the pair
        token_orders_to_try = [
            _ordered_tokens(addresses.diem_token, addresses.vvv_token)
        ]

    if pair_factory:
        notes.append(f"Pair factory(): {pair_factory}")
        if pair_factory.lower() != addresses.diem_vvv_factory.lower():
            notes.append(
                f"⚠️ Pair reports different factory ({pair_factory}) than configured ({addresses.diem_vvv_factory})"
            )
    else:
        notes.append("Pair factory() not readable")

    try:
        factory = get_contract(w3, addresses.diem_vvv_factory, "aerodrome_factory.json")
        # Try the configured stable flag first, then try the opposite if it doesn't match
        stable_flags_to_try = [addresses.diem_vvv_stable, not addresses.diem_vvv_stable]

        for token_a, token_b in token_orders_to_try:
            for stable_flag in stable_flags_to_try:
                try:
                    reported_raw = factory.functions.getPool(
                        token_a, token_b, stable_flag
                    ).call()
                    reported_candidate = _normalize_optional(reported_raw or "")
                    if (
                        reported_candidate
                        and reported_candidate.lower()
                        == addresses.diem_vvv_pair.lower()
                    ):
                        reported = reported_candidate
                        notes.append(
                            f"✓ Found pair with stable={stable_flag}, tokens={token_a[:10]}.../{token_b[:10]}..."
                        )
                        break
                    if reported_candidate:
                        notes.append(
                            f"Factory returned different pair (stable={stable_flag}): {reported_candidate}"
                        )
                except Exception as exc:
                    notes.append(
                        f"Factory lookup failed (stable={stable_flag}, tokens={token_a[:10]}.../{token_b[:10]}...): {exc}"
                    )
            if reported:
                break
    except Exception as exc:
        notes.append(f"Factory contract call failed: {exc}")
    return RegistrationStatus(
        factory=addresses.diem_vvv_factory,
        expected=addresses.diem_vvv_pair,
        reported=reported,
        registered=reported is not None
        and reported.lower() == addresses.diem_vvv_pair.lower(),
        notes=notes,
    )


def check_uniswap_v3_registration(w3, addresses: BridgeAddresses) -> RegistrationStatus:
    notes: list[str] = []
    reported: str | None = None
    try:
        factory = get_contract(
            w3, addresses.vvv_usdc_factory, "uniswap_v3_factory.json"
        )
        reported_raw = factory.functions.getPool(
            addresses.vvv_token, addresses.quote_token, addresses.vvv_usdc_fee
        ).call()
        reported = _normalize_optional(reported_raw or "")
    except Exception as exc:
        notes.append(f"Factory lookup failed: {exc}")
    token0, token1, pool_factory, fee = _describe_pool(w3, addresses.vvv_usdc_pool)
    if token0 and token1:
        notes.append(f"Pool tokens: token0={token0}, token1={token1}")
    else:
        notes.append("Unable to read token0/token1 from pool (pool may not exist)")
    if pool_factory:
        notes.append(f"Pool factory(): {pool_factory}")
    else:
        notes.append("Pool factory() not readable")
    if fee is not None:
        notes.append(f"Pool fee(): {fee}")
    return RegistrationStatus(
        factory=addresses.vvv_usdc_factory,
        expected=addresses.vvv_usdc_pool,
        reported=reported,
        registered=reported is not None
        and reported.lower() == addresses.vvv_usdc_pool.lower(),
        notes=notes,
    )


def _print_status(label: str, status: RegistrationStatus) -> None:
    print(f"\n=== {label} ===")
    print(f"Factory:  {status.factory}")
    print(f"Expected: {status.expected}")
    if status.reported:
        print(f"Factory get*: {status.reported}")
    else:
        print("Factory get*: <none>")
    print(f"Registered: {'yes' if status.registered else 'no'}")
    for note in status.notes:
        print(f"  - {note}")


def _require_live_guards(args: argparse.Namespace, w3, wallet_addr: str) -> None:
    chain_id = getattr(w3.eth, "chain_id", None)
    if chain_id != 8453:
        raise SystemExit(
            f"Live mode is only supported on Base mainnet (chain id 8453). Detected chain id: {chain_id}"
        )
    if (
        not args.confirm_mainnet
        and os.getenv(CONFIRM_MAINNET_ENV, "").strip().lower() != "yes"
    ):
        raise SystemExit("Live mode requires --confirm-mainnet or CONFIRM_MAINNET=YES")
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env != "production" and not args.allow_nonprod:
        raise SystemExit(
            "Set APP_ENV=production or pass --allow-nonprod to send transactions"
        )
    allowed_raw = os.getenv(ALLOWED_SENDERS_ENV, "")
    allowed = {
        value.strip().lower() for value in allowed_raw.split(",") if value.strip()
    }
    if (
        allowed
        and wallet_addr.lower() not in allowed
        and not args.allow_unlisted_sender
    ):
        raise SystemExit(
            f"Wallet {wallet_addr} not in {ALLOWED_SENDERS_ENV}. "
            "Add it to the allow-list or pass --allow-unlisted-sender."
        )


def _tx_data(factory_contract, fn_name: str, args: Sequence[object]) -> bytes:
    encoded = encode_contract_call(factory_contract, fn_name, list(args))
    if encoded.startswith("0x"):
        encoded = encoded[2:]
    return bytes.fromhex(encoded)


def _send_registration_tx(
    w3,
    wallet,
    factory_addr: str,
    factory_contract,
    fn_name: str,
    fn_args: Sequence[object],
    *,
    wait_for_receipt: bool,
) -> str:
    data = _tx_data(factory_contract, fn_name, fn_args)
    tx = build_eip1559_tx(w3, wallet.address, to=factory_addr, data=data)
    tx_hash = send_contract_tx(w3, wallet, tx)
    print(f"Submitted {fn_name} tx: {tx_hash}")
    if wait_for_receipt:
        print("Waiting for receipt...")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        status = getattr(receipt, "status", None)
        if status == 1:
            print("Transaction confirmed ✅")
        else:
            print(f"Transaction receipt returned status={status}")
    return tx_hash


def _has_code(w3, address: str) -> bool:
    try:
        code = w3.eth.get_code(address)
    except Exception:
        return False
    if isinstance(code, (bytes, bytearray)):
        return len(code) > 0
    if isinstance(code, str):
        return bool(code.strip("0x"))
    return False


def _ordered_tokens(*addresses: str) -> tuple[str, ...]:
    return tuple(sorted(addresses, key=lambda value: int(value.lower(), 16)))


def run(args: argparse.Namespace) -> None:
    load_dotenv_if_present(override=False)
    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    # Load Docker-specific env files (same pattern as apps/cli/main.py)
    docker_env = REPO_ROOT / ".env.docker"
    local_env = REPO_ROOT / "docker" / ".env.local"
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
    if local_env.exists():
        load_dotenv_if_present(path=str(local_env), override=True)

    w3 = get_web3()
    addresses = load_addresses()

    print("Base chain id:", w3.eth.chain_id)
    print(
        "Using RPC endpoint:",
        w3.provider.endpoint_uri
        if hasattr(w3.provider, "endpoint_uri")
        else "<unknown>",
    )

    aerodrome_status = check_aerodrome_registration(w3, addresses)
    uniswap_status = check_uniswap_v3_registration(w3, addresses)

    _print_status("Aerodrome DIEM/VVV pair", aerodrome_status)
    _print_status("Uniswap V3 VVV/USDC pool", uniswap_status)

    if not args.enable_live:
        print("\nDry run complete. No transactions were sent.")
        return

    wallet = get_account_wallet()
    print(f"\nSigner address: {wallet.address}")
    _require_live_guards(args, w3, wallet.address)

    if not aerodrome_status.registered:
        if _has_code(w3, addresses.diem_vvv_pair) and not args.force_registration:
            print(
                "\nAerodrome pair already has bytecode but factory lookup returned zero. "
                "Refusing to call createPool without --force-registration."
            )
        else:
            factory = get_contract(
                w3, addresses.diem_vvv_factory, "aerodrome_factory.json"
            )
            token_a, token_b = _ordered_tokens(
                addresses.diem_token, addresses.vvv_token
            )
            fn_args = (token_a, token_b, addresses.diem_vvv_stable)
            try:
                _send_registration_tx(
                    w3,
                    wallet,
                    addresses.diem_vvv_factory,
                    factory,
                    "createPool",
                    fn_args,
                    wait_for_receipt=args.wait_for_receipt,
                )
            except Exception as exc:
                print(f"Failed to call createPool: {exc}")
    else:
        print("\nAerodrome pair already registered. Skipping createPool.")

    if not uniswap_status.registered:
        if _has_code(w3, addresses.vvv_usdc_pool) and not args.force_registration:
            print(
                "\nUniswap V3 pool already has bytecode but factory lookup returned zero. "
                "Refusing to call createPool without --force-registration."
            )
        else:
            factory = get_contract(
                w3, addresses.vvv_usdc_factory, "uniswap_v3_factory.json"
            )
            token_a, token_b = _ordered_tokens(
                addresses.vvv_token, addresses.quote_token
            )
            fn_args = (token_a, token_b, addresses.vvv_usdc_fee)
            try:
                _send_registration_tx(
                    w3,
                    wallet,
                    addresses.vvv_usdc_factory,
                    factory,
                    "createPool",
                    fn_args,
                    wait_for_receipt=args.wait_for_receipt,
                )
            except Exception as exc:
                print(f"Failed to call createPool: {exc}")
    else:
        print("\nUniswap V3 pool already registered. Skipping createPool.")

    print("\nRun completed.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register bridge pools with their factories on Base."
    )
    parser.add_argument(
        "--enable-live",
        action="store_true",
        help="Send registration transactions (default is dry-run).",
    )
    parser.add_argument(
        "--confirm-mainnet",
        action="store_true",
        help="Required when --enable-live is set, unless CONFIRM_MAINNET=YES is exported.",
    )
    parser.add_argument(
        "--allow-nonprod",
        action="store_true",
        help="Allow live transactions when APP_ENV is not production.",
    )
    parser.add_argument(
        "--allow-unlisted-sender",
        action="store_true",
        help=f"Bypass the {ALLOWED_SENDERS_ENV} allow-list when sending transactions.",
    )
    parser.add_argument(
        "--force-registration",
        action="store_true",
        help="Attempt registration even if bytecode already exists at the expected pool address.",
    )
    parser.add_argument(
        "--wait-for-receipt",
        action="store_true",
        help="Wait for each submitted transaction to be mined (only meaningful with --enable-live).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        args = parse_args(argv)
        run(args)
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit("Interrupted by user")


if __name__ == "__main__":
    main()
