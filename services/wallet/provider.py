from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from libs.agentkit_ext.agentkit_wallet import get_agentkit_wallet
from libs.agentkit_ext.web3_utils import build_eip1559_tx, get_web3


class WalletError(Exception):
    """Raised when wallet operations fail or preconditions are not met."""


class WalletProvider(Protocol):
    def get_address(self) -> str: ...

    def send_transaction(self, tx: dict) -> str: ...


class TransactionSigner(Protocol):
    @property
    def address(self) -> str: ...

    def sign_transaction(self, tx: dict) -> Any: ...


def _to_checksum(address: str) -> str:
    from web3 import Web3  # type: ignore

    try:
        return Web3.to_checksum_address(address)
    except Exception as exc:  # noqa: BLE001
        raise WalletError(f"Invalid address: {address}") from exc


@dataclass
class AgentKitWalletAdapter:
    """Adapter exposing a minimal wallet interface over an AgentKit provider."""

    inner: WalletProvider
    kind: str
    _cached_address: Optional[str] = field(default=None, init=False, repr=False)

    @property
    def address(self) -> str:
        if self._cached_address is None:
            try:
                raw = self.inner.get_address()
            except Exception as exc:  # noqa: BLE001
                raise WalletError("Failed to fetch wallet address") from exc
            self._cached_address = _to_checksum(raw)
        return self._cached_address

    def sign_message(self, message: str) -> str:
        if not message:
            raise WalletError("Message is required for signing")
        if self.kind == "smart":
            owner_pk = os.getenv("OWNER")
            if not owner_pk or not owner_pk.startswith("0x"):
                raise WalletError(
                    "OWNER must be set to an EOA private key to sign when using smart_wallet"
                )
            from importlib import import_module

            Account = import_module("eth_account").Account  # type: ignore[attr-defined]
            encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]
            try:
                msg = encode_defunct(text=message)
                signed = Account.sign_message(msg, private_key=owner_pk)
            except Exception as exc:  # noqa: BLE001
                raise WalletError("Smart wallet owner failed to sign message") from exc
            return signed.signature.hex()

        signer = getattr(self.inner, "sign_message", None)
        if callable(signer):
            try:
                sig = signer(message)
            except Exception as exc:  # noqa: BLE001
                raise WalletError("Hot wallet failed to sign message") from exc
            return sig.hex() if isinstance(sig, bytes) else str(sig)

        pk = os.getenv("ETH_PRIVATE_KEY")
        if not pk:
            raise WalletError("ETH_PRIVATE_KEY is required to sign message with eth_account provider")
        from importlib import import_module

        Account = import_module("eth_account").Account  # type: ignore[attr-defined]
        encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]
        try:
            msg = encode_defunct(text=message)
            signed = Account.sign_message(msg, private_key=pk)
        except Exception as exc:  # noqa: BLE001
            raise WalletError("Failed to sign message with ETH_PRIVATE_KEY") from exc
        return signed.signature.hex()

    def send_transaction(
        self,
        *,
        to: str,
        data: Optional[bytes] = None,
        value: int = 0,
        gas_limit: Optional[int] = None,
        max_fee_per_gas: Optional[int] = None,
        max_priority_fee_per_gas: Optional[int] = None,
    ) -> str:
        if value < 0:
            raise WalletError("Transaction value cannot be negative")
        payload: dict[str, Any] = {"value": int(value)}
        if to:
            payload["to"] = _to_checksum(to)
        if data is not None:
            payload["data"] = data
        if gas_limit is not None:
            payload["gas"] = int(gas_limit)
        if max_fee_per_gas is not None:
            payload["maxFeePerGas"] = int(max_fee_per_gas)
        if max_priority_fee_per_gas is not None:
            payload["maxPriorityFeePerGas"] = int(max_priority_fee_per_gas)
        try:
            tx_hash = self.inner.send_transaction(payload)
        except Exception as exc:  # noqa: BLE001
            raise WalletError("Hot wallet failed to send transaction") from exc
        return str(tx_hash)


@dataclass
class LocalAccountSigner:
    """Local EOA signer used for cold wallet operations."""

    private_key: str
    _account: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from importlib import import_module

        Account = import_module("eth_account").Account  # type: ignore[attr-defined]
        try:
            self._account = Account.from_key(self.private_key)
        except Exception as exc:  # noqa: BLE001
            raise WalletError("Invalid cold wallet private key") from exc

    @property
    def address(self) -> str:
        return _to_checksum(self._account.address)

    def sign_transaction(self, tx: dict) -> Any:
        try:
            return self._account.sign_transaction(tx)
        except Exception as exc:  # noqa: BLE001
            raise WalletError("Cold wallet failed to sign transaction") from exc


def _cold_signer_from_env() -> LocalAccountSigner:
    pk = os.getenv("COLD_WALLET_PRIVATE_KEY") or os.getenv("ETH_COLD_PRIVATE_KEY")
    if not pk:
        raise WalletError("Set COLD_WALLET_PRIVATE_KEY for cold wallet operations")
    return LocalAccountSigner(pk)


def transfer_from_cold_to_hot(amount_wei: int, cold_signer: Optional[TransactionSigner] = None) -> str:
    if amount_wei <= 0:
        raise WalletError("amount_wei must be positive")
    signer = cold_signer or _cold_signer_from_env()
    provider = get_default_provider()
    w3 = get_web3()
    tx = build_eip1559_tx(w3, from_addr=signer.address, to=provider.address, value=int(amount_wei))
    try:
        signed = signer.sign_transaction(tx)
        raw_tx = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        if raw_tx is None:
            raise WalletError("Signer did not produce rawTransaction")
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
    except WalletError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WalletError("Failed to bridge funds from cold wallet") from exc
    return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)


def _default_gas_buffer(w3) -> int:
    try:
        gas_price = int(w3.eth.gas_price)
    except Exception:  # noqa: BLE001
        gas_price = 0
    # 21k gas for plain transfers with 20% headroom
    return int(gas_price * 21000 * 1.2) if gas_price else 0


def sweep_profits_to_cold(
    min_balance_wei: int,
    *,
    cold_address: Optional[str] = None,
    gas_buffer_wei: Optional[int] = None,
) -> str:
    if min_balance_wei < 0:
        raise WalletError("min_balance_wei cannot be negative")
    provider = get_default_provider()
    w3 = get_web3()
    hot_addr = provider.address
    try:
        balance = int(w3.eth.get_balance(hot_addr))
    except Exception as exc:  # noqa: BLE001
        raise WalletError("Failed to load hot wallet balance") from exc
    if balance <= min_balance_wei:
        raise WalletError("Hot wallet balance does not exceed reserve threshold")
    dest = cold_address or os.getenv("COLD_WALLET_ADDRESS")
    if not dest:
        raise WalletError("Cold wallet address is required via argument or COLD_WALLET_ADDRESS")
    checksum_dest = _to_checksum(dest)
    buffer = gas_buffer_wei
    if buffer is None:
        env_buffer = os.getenv("WALLET_SWEEP_GAS_BUFFER_WEI")
        if env_buffer:
            try:
                buffer = int(env_buffer)
            except ValueError as exc:  # noqa: BLE001
                raise WalletError("WALLET_SWEEP_GAS_BUFFER_WEI must be an integer") from exc
        else:
            buffer = _default_gas_buffer(w3)
    transferable = balance - min_balance_wei - max(buffer or 0, 0)
    if transferable <= 0:
        raise WalletError("No funds available after reserving gas and minimum balance")
    return provider.send_transaction(to=checksum_dest, value=int(transferable))


def get_default_provider() -> AgentKitWalletAdapter:
    provider, kind = get_agentkit_wallet()
    return AgentKitWalletAdapter(inner=provider, kind=kind)  # type: ignore[arg-type]
