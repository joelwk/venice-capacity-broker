from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time optional
    from web3 import Web3  # type: ignore
    from web3.contract import Contract  # type: ignore


ABI_DIR = Path(__file__).resolve().parents[2] / "abi"


def load_abi(name: str) -> List[Dict[str, Any]]:
    fp = ABI_DIR / name
    if not fp.exists():
        raise FileNotFoundError(f"ABI not found: {fp}")
    return json.loads(fp.read_text())


def _split_urls(raw: str | None) -> list[str]:
    if not raw:
        return []
    urls: list[str] = []
    for candidate in raw.split(','):
        url = candidate.strip()
        if url:
            urls.append(url)
    return urls


def rpc_url_candidates() -> list[str]:
    """Return RPC endpoints in preference order."""

    urls: list[str] = []
    urls.extend(_split_urls(os.getenv("RPC_URLS")))
    urls.extend(_split_urls(os.getenv("BASE_RPC_URLS")))
    for key in ("RPC_URL", "BASE_RPC_URL"):
        val = (os.getenv(key) or "").strip()
        if val:
            urls.append(val)
    if not urls:
        raise EnvironmentError("RPC_URL or BASE_RPC_URL (or *_URLS) is required for Web3 operations")
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            ordered.append(url)
            seen.add(url)
    return ordered


def resolve_rpc_url(validate: bool = False) -> str:
    """Return the first usable RPC URL, optionally validating connectivity."""

    candidates = rpc_url_candidates()
    if not validate:
        return candidates[0]
    errors: list[str] = []
    from web3 import Web3  # type: ignore

    for rpc in candidates:
        provider = Web3.HTTPProvider(rpc)
        w3 = Web3(provider)
        try:
            if not w3.is_connected():
                raise ConnectionError("not reachable")
            # Access a cheap RPC to ensure the node responds correctly
            _ = w3.eth.chain_id
            return rpc
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rpc}: {exc}")
            continue
    raise ConnectionError("Failed to connect to any RPC endpoint: " + "; ".join(errors))


def get_web3() -> 'Web3':
    from web3 import Web3  # type: ignore

    rpc = resolve_rpc_url(validate=True)
    provider = Web3.HTTPProvider(rpc)
    w3 = Web3(provider)
    # resolve_rpc_url already verified connectivity, but keep a safeguard
    if not w3.is_connected():
        raise ConnectionError(f"Failed to connect to RPC: {rpc}")
    return w3


@dataclass
class Wallet:
    private_key: str

    @property
    def address(self) -> str:
        from importlib import import_module

        Account = import_module("eth_account").Account  # type: ignore[attr-defined]
        return Account.from_key(self.private_key).address

    def sign_message(self, message: str) -> str:
        from importlib import import_module

        Account = import_module("eth_account").Account  # type: ignore[attr-defined]
        encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]
        msg = encode_defunct(text=message)
        signed = Account.sign_message(msg, private_key=self.private_key)
        return signed.signature.hex()


def get_account_wallet() -> Wallet:
    pk = os.getenv("ETH_PRIVATE_KEY")
    if not pk:
        raise EnvironmentError("ETH_PRIVATE_KEY is required for signing and transactions")
    return Wallet(private_key=pk)


def get_contract(w3: 'Web3', address: str, abi_name: str) -> 'Contract':
    from web3 import Web3  # type: ignore
    abi = load_abi(abi_name)
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


def build_eip1559_tx(w3: 'Web3', from_addr: str, to: Optional[str] = None, value: int = 0, data: Optional[bytes] = None) -> Dict[str, Any]:
    base_fee = w3.eth.get_block("latest").baseFeePerGas
    max_priority = w3.to_wei(1, "gwei")
    max_fee = int(base_fee * 2) + max_priority
    tx: Dict[str, Any] = {
        "chainId": w3.eth.chain_id,
        "from": Web3.to_checksum_address(from_addr),
        "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(from_addr)),
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
        "gas": 0,  # to be estimated
        "value": value,
    }
    if to:
        tx["to"] = Web3.to_checksum_address(to)
    if data:
        tx["data"] = data
    # Estimate gas
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    return tx


def send_contract_tx(w3: 'Web3', wallet: Wallet, tx: Dict[str, Any]) -> str:
    signed = w3.eth.account.sign_transaction(tx, wallet.private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    return tx_hash.hex()
