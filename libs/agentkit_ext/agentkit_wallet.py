from __future__ import annotations

import os
import re
from typing import Any

try:
    from web3 import Web3  # type: ignore
except Exception:  # pragma: no cover - optional dependency in unit tests
    Web3 = None  # type: ignore[assignment]

from libs.runtime.preflight import DEFAULT_INSTALL_HINT


def _must(env: str) -> str:
    v = os.getenv(env)
    if not v:
        raise OSError(f"Missing required environment variable: {env}")
    return v


def _require_base_network() -> str:
    """Return a supported Base network id, or raise.

    Prefers `NETWORK_ID` if set, otherwise maps `BASE_CHAIN_ID` to a default.
    Supported: 'base-mainnet', 'base-sepolia'.
    """
    network_id = os.getenv("NETWORK_ID")
    if network_id:
        if network_id not in {"base-mainnet", "base-sepolia"}:
            raise OSError("NETWORK_ID must be 'base-mainnet' or 'base-sepolia'")
        return network_id
    chain_id = os.getenv("BASE_CHAIN_ID", "8453")
    if chain_id == "8453":
        return "base-mainnet"
    if chain_id in {"84532", "84531"}:
        return "base-sepolia"
    raise OSError("BASE_CHAIN_ID must be 8453 (base-mainnet) or 84532 (base-sepolia)")


def get_agentkit_wallet() -> tuple[object, str]:
    """Instantiate an AgentKit wallet provider based on env.

    Returns a tuple (provider, kind) where kind is 'smart' or 'eth'.
    """
    try:
        import coinbase_agentkit.wallet_providers as agentkit_providers
    except ModuleNotFoundError as exc:
        hint = os.getenv("AGENTKIT_INSTALL_HINT") or DEFAULT_INSTALL_HINT
        raise OSError(
            "coinbase-agentkit is required for Base wallet operations. "
            f"Install via `{hint}` or set TREASURY_ADDRESS/COLD_WALLET_ADDRESS for read-only flows."
        ) from exc

    provider = os.getenv("WALLET_PROVIDER", "eth_account").lower()
    if provider == "smart_wallet":
        # Lazy import to avoid hard dependency if unused, handle API name variations across versions
        try:
            _SmartWalletProvider = agentkit_providers.SmartWalletProvider
            _SmartWalletProviderConfig = agentkit_providers.SmartWalletProviderConfig
        except Exception:
            _SmartWalletProvider = agentkit_providers.CdpSmartWalletProvider  # type: ignore[assignment]
            _SmartWalletProviderConfig = agentkit_providers.CdpSmartWalletProviderConfig  # type: ignore[assignment]

        network_id = _require_base_network()
        # Build kwargs defensively based on available fields across versions
        fields = getattr(_SmartWalletProviderConfig, "model_fields", {})  # type: ignore[attr-defined]
        kwargs: dict[str, Any] = {"network_id": network_id}
        if "api_key_id" in fields:
            kwargs["api_key_id"] = _must("CDP_API_KEY_ID")
        if "api_key_secret" in fields:
            kwargs["api_key_secret"] = _must("CDP_API_KEY_SECRET")
        if "wallet_secret" in fields:
            kwargs["wallet_secret"] = _must("CDP_WALLET_SECRET")
        if "cdp_api_key_name" in fields:
            kwargs["cdp_api_key_name"] = os.getenv("CDP_API_KEY_ID")
        if "cdp_api_key_private_key" in fields:
            kwargs["cdp_api_key_private_key"] = os.getenv("CDP_API_KEY_SECRET")
        if "owner" in fields:
            _owner = os.getenv("OWNER")
            if _owner:
                kwargs["owner"] = _owner
        if "paymaster_url" in fields:
            kwargs["paymaster_url"] = os.getenv("PAYMASTER_URL")
        if "rpc_url" in fields:
            kwargs["rpc_url"] = os.getenv("BASE_RPC_URL")

        cfg = _SmartWalletProviderConfig(**kwargs)
        return _SmartWalletProvider(cfg), "smart"

    # Default: eth account
    EthAccountWalletProvider = agentkit_providers.EthAccountWalletProvider
    EthAccountWalletProviderConfig = agentkit_providers.EthAccountWalletProviderConfig

    # Lazy import for eth_account
    from importlib import import_module

    Account = import_module("eth_account").Account  # type: ignore[attr-defined]
    account = Account.from_key(_must("ETH_PRIVATE_KEY"))
    # enforce Base-only
    _require_base_network()
    chain_id = os.getenv("BASE_CHAIN_ID", "8453")
    cfg = EthAccountWalletProviderConfig(
        account=account,
        chain_id=str(chain_id),
        rpc_url=os.getenv("BASE_RPC_URL"),
    )
    return EthAccountWalletProvider(cfg), "eth"


def _normalize_gas_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
    if not overrides:
        return {}
    normalized: dict[str, Any] = {}
    for key, raw_value in overrides.items():
        if raw_value is None:
            continue
        try:
            normalized[key] = int(raw_value)
        except Exception:
            normalized[key] = raw_value
    return normalized


_HEX_ADDR_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _maybe_checksum_address(addr: str) -> str:
    """Return a checksummed EVM address when it looks like a 20-byte hex string.

    web3.py v7 rejects non-checksummed addresses in tx dicts. Keep this
    conservative so unit tests using short/demo addresses don't break.
    """
    if not addr:
        return addr
    cleaned = str(addr).strip()
    if not cleaned:
        return cleaned
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    if cleaned.startswith(("0x", "0X")):
        prefixed = "0x" + cleaned[2:]
        body = prefixed[2:]
    else:
        body = cleaned
        prefixed = "0x" + body
    if len(body) != 40 or _HEX_ADDR_RE.fullmatch(body) is None:
        return cleaned
    if Web3 is None:
        return prefixed
    return Web3.to_checksum_address(prefixed)


def _get_base_gas_defaults() -> dict[str, int]:
    """Get sensible gas defaults for Base chain.

    Base gas is extremely cheap (~0.001 gwei), but some RPC providers return
    inflated estimates. This provides reasonable caps to prevent overpaying.
    """
    # Base typical gas: 0.001-0.01 gwei
    # We use 0.1 gwei as a generous cap (100x typical) to handle spikes
    # This is still ~1000x cheaper than Ethereum mainnet
    max_priority_fee = int(
        os.getenv("BASE_MAX_PRIORITY_FEE_WEI", "100000000")
    )  # 0.1 gwei
    max_fee = int(os.getenv("BASE_MAX_FEE_PER_GAS_WEI", "200000000"))  # 0.2 gwei
    return {
        "maxPriorityFeePerGas": max_priority_fee,
        "maxFeePerGas": max_fee,
    }


def send_tx(
    to: str,
    data: bytes | None = None,
    value: int = 0,
    *,
    gas_overrides: dict[str, Any] | None = None,
) -> str:
    """Send a transaction via the active AgentKit wallet provider.

    Returns a transaction hash hex string.
    """
    provider, kind = get_agentkit_wallet()
    tx = {"to": _maybe_checksum_address(to), "value": value}
    if data:
        # web3.py provider expects hex str or bytes; AgentKit SmartWallet expects bytes/hex
        tx["data"] = data

    # Auto-apply Base gas caps if no overrides provided to avoid inflated RPC estimates
    # This forces the _send_with_eth_account path which respects our caps
    overrides = _normalize_gas_overrides(gas_overrides)
    if not overrides:
        chain_id = int(os.getenv("BASE_CHAIN_ID", "8453"))
        try:
            provider_chain = getattr(
                getattr(provider, "_network", None), "chain_id", None
            )
            if provider_chain:
                chain_id = int(provider_chain)
        except Exception:
            pass
        # Apply Base gas defaults on Base chain (8453)
        if chain_id == 8453:
            overrides = _get_base_gas_defaults()

    if overrides:
        tx.update(overrides)

    def _send_with_eth_account() -> str:
        """Respect explicit gas overrides when using EthAccountWalletProvider.

        The upstream provider recomputes fees internally, which ignores our caps.
        We sign and send raw here so maxFeePerGas/maxPriorityFeePerGas are honored.
        """

        web3 = getattr(provider, "web3", None)
        account = getattr(provider, "account", None)
        if web3 is None or account is None:
            return provider.send_transaction(tx)  # type: ignore[attr-defined]

        tx.setdefault("chainId", int(provider._network.chain_id))  # type: ignore[attr-defined]
        tx.setdefault("from", account.address)
        if "nonce" not in tx:
            # Use "pending" to include unconfirmed transactions in nonce count.
            # This is critical for multi-leg composite trades where leg 2 must
            # use a nonce higher than leg 1's pending transaction.
            tx["nonce"] = web3.eth.get_transaction_count(account.address, "pending")
        if "gas" not in tx:
            gas_mult = getattr(provider, "_gas_limit_multiplier", 1) or 1
            estimated = web3.eth.estimate_gas(tx)
            tx["gas"] = int(estimated * gas_mult)
        signed = account.sign_transaction(tx)
        # Handle both old (rawTransaction) and new (raw_transaction) attribute naruff mes
        raw_tx = getattr(signed, "raw_transaction", None) or getattr(
            signed, "rawTransaction", None
        )
        if raw_tx is None:
            raise AttributeError(
                "SignedTransaction has neither 'raw_transaction' nor 'rawTransaction'"
            )
        tx_hash = web3.eth.send_raw_transaction(raw_tx)
        if Web3 is not None:
            return Web3.to_hex(tx_hash)
        try:
            return "0x" + tx_hash.hex()
        except Exception:
            return str(tx_hash)

    # If caller set gas price caps, bypass provider defaults that overwrite them.
    if overrides and any(
        k in overrides for k in ("maxFeePerGas", "maxPriorityFeePerGas", "gasPrice")
    ):
        return _send_with_eth_account()

    # coinbase-agentkit providers expose send_transaction
    return provider.send_transaction(tx)  # type: ignore[attr-defined]


def wait_for_tx_confirmation(
    tx_hash: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Wait for a transaction to be confirmed on-chain.

    Returns a dict with:
        - status: 'confirmed' (success), 'failed' (reverted), or 'timeout'
        - tx_hash: the transaction hash
        - receipt: the full receipt dict (if confirmed/failed)
        - block_number: the block number (if confirmed/failed)

    This is critical for multi-step flows like mint-then-sell where
    the second transaction depends on the first being mined.
    """
    from libs.agentkit_ext.web3_utils import get_web3

    w3 = get_web3()
    try:
        tx_hash_bytes = (
            bytes.fromhex(tx_hash[2:])
            if tx_hash.startswith("0x")
            else bytes.fromhex(tx_hash)
        )
    except ValueError as exc:
        # Handle malformed hashes gracefully in tests or dry-runs.
        return {
            "status": "invalid_hash",
            "tx_hash": tx_hash,
            "error": str(exc),
        }

    import time

    start = time.time()
    while time.time() - start < timeout:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash_bytes)
            if receipt is not None:
                status = receipt.get("status", 0)
                return {
                    "status": "confirmed" if status == 1 else "failed",
                    "tx_hash": tx_hash,
                    "receipt": dict(receipt),
                    "block_number": receipt.get("blockNumber"),
                }
        except Exception:
            pass  # Transaction not yet mined
        time.sleep(poll_interval)

    return {"status": "timeout", "tx_hash": tx_hash, "timeout_seconds": timeout}


def get_address() -> str:
    provider, _ = get_agentkit_wallet()
    return provider.get_address()  # type: ignore[attr-defined]


def sign_message(message: str) -> str:
    """Sign a message.

    - For eth_account provider: delegate to provider.sign_message.
    - For smart_wallet: sign using OWNER EOA private key (required) since
      smart wallets cannot sign arbitrary messages directly.
    """
    provider, kind = get_agentkit_wallet()
    if kind == "smart":
        owner_pk = os.getenv("OWNER")
        if not owner_pk or not owner_pk.startswith("0x"):
            raise OSError(
                "OWNER must be set to an EOA private key (0x...) to sign challenges when using smart_wallet"
            )
        # Lazy imports for signing
        from importlib import import_module

        Account = import_module("eth_account").Account  # type: ignore[attr-defined]
        encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]

        msg = encode_defunct(text=message)
        signed = Account.sign_message(msg, private_key=owner_pk)
        return signed.signature.hex()

    # eth_account path: prefer provider if available, else local sign
    if hasattr(provider, "sign_message"):
        return provider.sign_message(message)  # type: ignore[attr-defined]

    # Fallback local signing with ETH_PRIVATE_KEY
    pk = os.getenv("ETH_PRIVATE_KEY")
    if not pk:
        raise OSError(
            "ETH_PRIVATE_KEY is required to sign message with eth_account provider"
        )
    from importlib import import_module

    Account = import_module("eth_account").Account  # type: ignore[attr-defined]
    encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]
    msg = encode_defunct(text=message)
    signed = Account.sign_message(msg, private_key=pk)
    return signed.signature.hex()
