from __future__ import annotations

import os
from typing import Any, Optional, Tuple


def _must(env: str) -> str:
    v = os.getenv(env)
    if not v:
        raise EnvironmentError(f"Missing required environment variable: {env}")
    return v


def _require_base_network() -> str:
    """Return a supported Base network id, or raise.

    Prefers `NETWORK_ID` if set, otherwise maps `BASE_CHAIN_ID` to a default.
    Supported: 'base-mainnet', 'base-sepolia'.
    """
    network_id = os.getenv("NETWORK_ID")
    if network_id:
        if network_id not in {"base-mainnet", "base-sepolia"}:
            raise EnvironmentError("NETWORK_ID must be 'base-mainnet' or 'base-sepolia'")
        return network_id
    chain_id = os.getenv("BASE_CHAIN_ID", "8453")
    if chain_id == "8453":
        return "base-mainnet"
    if chain_id in {"84532", "84531"}:
        return "base-sepolia"
    raise EnvironmentError("BASE_CHAIN_ID must be 8453 (base-mainnet) or 84532 (base-sepolia)")


def get_agentkit_wallet() -> Tuple[object, str]:
    """Instantiate an AgentKit wallet provider based on env.

    Returns a tuple (provider, kind) where kind is 'smart' or 'eth'.
    """
    provider = os.getenv("WALLET_PROVIDER", "eth_account").lower()
    if provider == "smart_wallet":
        # Lazy import to avoid hard dependency if unused, handle API name variations across versions
        try:
            from coinbase_agentkit.wallet_providers import (
                SmartWalletProvider as _SmartWalletProvider,
                SmartWalletProviderConfig as _SmartWalletProviderConfig,
            )
        except Exception:
            from coinbase_agentkit.wallet_providers import (  # type: ignore[assignment]
                CdpSmartWalletProvider as _SmartWalletProvider,
                CdpSmartWalletProviderConfig as _SmartWalletProviderConfig,
            )

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
    from coinbase_agentkit.wallet_providers import (
        EthAccountWalletProvider,
        EthAccountWalletProviderConfig,
    )

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


def _normalize_gas_overrides(overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
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


def send_tx(
    to: str,
    data: Optional[bytes] = None,
    value: int = 0,
    *,
    gas_overrides: Optional[dict[str, Any]] = None,
) -> str:
    """Send a transaction via the active AgentKit wallet provider.

    Returns a transaction hash hex string.
    """
    provider, kind = get_agentkit_wallet()
    tx = {"to": to, "value": value}
    if data:
        # web3.py provider expects hex str or bytes; AgentKit SmartWallet expects bytes/hex
        tx["data"] = data
    overrides = _normalize_gas_overrides(gas_overrides)
    if overrides:
        tx.update(overrides)
    # coinbase-agentkit providers expose send_transaction
    return provider.send_transaction(tx)  # type: ignore[attr-defined]


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
            raise EnvironmentError(
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
        raise EnvironmentError("ETH_PRIVATE_KEY is required to sign message with eth_account provider")
    from importlib import import_module

    Account = import_module("eth_account").Account  # type: ignore[attr-defined]
    encode_defunct = import_module("eth_account.messages").encode_defunct  # type: ignore[attr-defined]
    msg = encode_defunct(text=message)
    signed = Account.sign_message(msg, private_key=pk)
    return signed.signature.hex()
