#!/usr/bin/env python
"""Operator-facing wallet CLI for hot/cold management."""

from __future__ import annotations

import argparse
import binascii
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:  # Load repo-level .env so CLI picks up expected configuration
    from libs.env import load_dotenv_if_present  # type: ignore

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
except Exception:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=str(REPO_ROOT / ".env"), override=False)
    except Exception:
        pass

from services.wallet.provider import (  # noqa: E402  (local import after sys.path)
    LocalAccountSigner,
    WalletError,
    get_default_provider,
    sweep_profits_to_cold,
    transfer_from_cold_to_hot,
)


def _parse_wei(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise WalletError(
            "value must be an integer (optionally prefixed with 0x)"
        ) from exc


def _hex_to_bytes(raw: str) -> bytes:
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) % 2:
        raw = "0" + raw
    try:
        return binascii.unhexlify(raw)
    except binascii.Error as exc:
        raise WalletError("data must be hex-encoded") from exc


def cmd_address(_: argparse.Namespace) -> None:
    provider = get_default_provider()
    print(provider.address)


def cmd_sign(args: argparse.Namespace) -> None:
    provider = get_default_provider()
    sig = provider.sign_message(args.message)
    print(sig)


def cmd_send(args: argparse.Namespace) -> None:
    provider = get_default_provider()
    data = _hex_to_bytes(args.data) if args.data else None
    tx_hash = provider.send_transaction(
        to=args.to, data=data, value=_parse_wei(args.value)
    )
    print(tx_hash)


def cmd_transfer_cold(args: argparse.Namespace) -> None:
    signer = LocalAccountSigner(args.cold_key) if args.cold_key else None
    tx_hash = transfer_from_cold_to_hot(_parse_wei(args.amount), cold_signer=signer)
    print(tx_hash)


def cmd_sweep(args: argparse.Namespace) -> None:
    gas_buffer = int(args.gas_buffer) if args.gas_buffer is not None else None
    tx_hash = sweep_profits_to_cold(
        _parse_wei(args.min_balance),
        cold_address=args.cold_address,
        gas_buffer_wei=gas_buffer,
    )
    print(tx_hash)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wallet management CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("address", help="Print the active hot wallet address")
    sp.set_defaults(func=cmd_address)

    sp = sub.add_parser("sign", help="Sign an arbitrary message with the hot wallet")
    sp.add_argument("message", help="Plaintext message to sign")
    sp.set_defaults(func=cmd_sign)

    sp = sub.add_parser("send", help="Send a raw transaction via the hot wallet")
    sp.add_argument("--to", required=True, help="Destination address")
    sp.add_argument("--value", required=True, help="Value in wei")
    sp.add_argument("--data", default=None, help="Optional hex data payload")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser(
        "transfer-cold",
        help="Bridge funds from a cold wallet (private key) into the hot wallet",
    )
    sp.add_argument("amount", help="Amount in wei to transfer into the hot wallet")
    sp.add_argument(
        "--cold-key",
        dest="cold_key",
        default=None,
        help="Cold wallet private key (defaults to COLD_WALLET_PRIVATE_KEY env)",
    )
    sp.set_defaults(func=cmd_transfer_cold)

    sp = sub.add_parser(
        "sweep",
        help="Sweep excess hot wallet balance back to the configured cold wallet address",
    )
    sp.add_argument(
        "min_balance",
        help="Minimum wei balance to keep in the hot wallet after sweeping",
    )
    sp.add_argument(
        "--cold-address",
        dest="cold_address",
        default=None,
        help="Cold wallet address (defaults to COLD_WALLET_ADDRESS env)",
    )
    sp.add_argument(
        "--gas-buffer",
        dest="gas_buffer",
        default=None,
        help="Override gas buffer in wei (defaults to WALLET_SWEEP_GAS_BUFFER_WEI or auto)",
    )
    sp.set_defaults(func=cmd_sweep)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except WalletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
