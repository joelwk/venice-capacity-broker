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


def get_web3() -> 'Web3':
    from web3 import Web3  # type: ignore
    # Prefer generic RPC_URL if provided; fall back to BASE_RPC_URL
    rpc = os.getenv("RPC_URL") or os.getenv("BASE_RPC_URL")
    if not rpc:
        raise EnvironmentError("RPC_URL or BASE_RPC_URL is required for Web3 operations")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():  # web3.py>=6 uses camelCase
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
