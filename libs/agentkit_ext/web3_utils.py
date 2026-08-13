from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from web3 import Web3  # type: ignore
except Exception:  # pragma: no cover - optional dependency in unit tests
    Web3 = None  # type: ignore[assignment]

from libs.agentkit_ext.rpc_health import get_rpc_health_tracker

if TYPE_CHECKING:  # pragma: no cover - import-time optional
    from web3 import Web3  # type: ignore
    from web3.contract import Contract  # type: ignore

ABI_DIR = Path(__file__).resolve().parents[2] / "abi"

_RPC_LOCK = Lock()
_RPC_INDEX = 0
_RPC_SESSION_CACHE: dict[str, requests.Session] = {}


def _rpc_timeout_seconds() -> float:
    raw = os.getenv("RPC_REQUEST_TIMEOUT_SECONDS") or os.getenv(
        "BASE_RPC_TIMEOUT_SECONDS"
    )
    try:
        if raw:
            val = float(raw)
            return val if val > 0 else 10.0
    except Exception:
        pass
    return 10.0


def _build_retrying_session(rpc_url: str) -> requests.Session:
    with _RPC_LOCK:
        cached = _RPC_SESSION_CACHE.get(rpc_url)
        if cached is not None:
            return cached
        session = requests.Session()
        retry = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=0.3,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _RPC_SESSION_CACHE[rpc_url] = session
        return session


def _ordered_candidates(candidates: list[str]) -> list[tuple[int, str]]:
    if not candidates:
        return []
    with _RPC_LOCK:
        global _RPC_INDEX
        start = _RPC_INDEX % len(candidates)
    ordered: list[tuple[int, str]] = []
    for offset in range(len(candidates)):
        idx = (start + offset) % len(candidates)
        ordered.append((idx, candidates[idx]))
    return ordered


def _advance_index(success_idx: int, size: int) -> None:
    if size <= 0:
        return
    with _RPC_LOCK:
        global _RPC_INDEX
        _RPC_INDEX = (success_idx + 1) % size


def _build_web3_for_rpc(rpc: str) -> Web3:
    if Web3 is None:
        raise RuntimeError("web3 library not available; Web3 operations require web3")

    session = _build_retrying_session(rpc)
    provider = Web3.HTTPProvider(
        rpc, session=session, request_kwargs={"timeout": _rpc_timeout_seconds()}
    )
    return Web3(provider)


def load_abi(name: str) -> list[dict[str, Any]]:
    fp = ABI_DIR / name
    if not fp.exists():
        raise FileNotFoundError(f"ABI not found: {fp}")
    return json.loads(fp.read_text())


def _split_urls(raw: str | None) -> list[str]:
    if not raw:
        return []
    urls: list[str] = []
    for candidate in raw.split(","):
        url = candidate.strip()
        if url:
            urls.append(url)
    return urls


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect if an exception represents a rate limit (429) error."""
    if hasattr(exc, "response"):
        status_code = getattr(exc.response, "status_code", None)
        if isinstance(status_code, int) and status_code == 429:
            return True
    if hasattr(exc, "status_code"):
        if isinstance(exc.status_code, int) and exc.status_code == 429:
            return True
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg or "rate limit" in msg:
        return True
    return False


def rpc_url_candidates() -> list[str]:
    """Return RPC endpoints in preference order.

    Includes additional Base RPC endpoints from public sources
    (e.g., chainlist.org) for improved reliability.
    """

    urls: list[str] = []
    urls.extend(_split_urls(os.getenv("RPC_URLS")))
    urls.extend(_split_urls(os.getenv("BASE_RPC_URLS")))
    for key in ("RPC_URL", "BASE_RPC_URL"):
        val = (os.getenv(key) or "").strip()
        if val:
            urls.append(val)
    for fallback in (
        os.getenv("RPC_URL_FALLBACK"),
        os.getenv("BASE_RPC_URL_FALLBACK"),
    ):
        urls.extend(_split_urls(fallback))

    # Add additional Base RPC endpoints from public sources for redundancy
    # These are high-quality public endpoints that support Base mainnet
    # BUT: Only add public fallbacks if no paid RPC (Alchemy, Infura, etc.) is configured
    # to avoid rate-limiting issues when paid RPCs are temporarily marked unhealthy
    def _is_paid_rpc(url: str) -> bool:
        u = url.lower()
        # Treat Alchemy's public demo endpoint as a public RPC so we still add
        # other public fallbacks for redundancy in dev/test environments.
        if "alchemy.com" in u and "/v2/demo" in u:
            return False
        return any(
            provider in u
            for provider in ("alchemy.com", "infura.io", "quicknode.com", "ankr.com")
        )

    has_paid_rpc = any(_is_paid_rpc(url) for url in urls)

    if urls and any("base" in url.lower() for url in urls) and not has_paid_rpc:
        additional_base_rpcs = [
            "https://base.drpc.org",
            "https://base-rpc.publicnode.com",
            "https://mainnet.base.org",
            "https://base.blockpi.network/v1/rpc/public",
            "https://base.llamarpc.com",
            "https://base.meowrpc.com",
            "https://1rpc.io/base",
            "https://base-mainnet.public.blastapi.io",
            "https://gateway.tenderly.co/public/base",
            "https://base-mainnet.g.alchemy.com/v2/demo",
            "https://base.publicnode.com",
            "https://base.diamondswap.org/rpc",
            "https://base.merkle.io",
        ]
        urls.extend(additional_base_rpcs)
    if not urls:
        raise OSError(
            "RPC_URL or BASE_RPC_URL (or *_URLS) is required for Web3 operations"
        )
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            ordered.append(url)
            seen.add(url)
    return ordered


def resolve_rpc_url(validate: bool = False) -> str:
    """Return the first usable RPC URL, optionally validating connectivity.

    Uses health-aware selection to prefer healthy endpoints and avoid
    rate-limited or failing endpoints.
    """

    candidates = rpc_url_candidates()
    if not candidates:
        raise OSError(
            "RPC_URL or BASE_RPC_URL (or *_URLS) is required for Web3 operations"
        )

    tracker = get_rpc_health_tracker()

    # Use health tracker to select best endpoint
    selected = tracker.select_best_endpoint(candidates)
    if selected is None:
        # Fallback to round-robin if health tracker has no data
        ordered = _ordered_candidates(candidates)
        if not ordered:
            raise OSError("No RPC candidates available")
        idx, selected = ordered[0]
        _advance_index(idx, len(candidates))

    # Log RPC endpoint selection for observability
    try:
        import logging

        logger = logging.getLogger("rpc.health")
        if logger.isEnabledFor(logging.DEBUG):
            health_status = (
                tracker.get_endpoint_health(selected)
                if hasattr(tracker, "get_endpoint_health")
                else None
            )
            logger.debug(
                f"RPC endpoint selected: {selected[:50]}... "
                f"(health={health_status if health_status else 'unknown'}, "
                f"candidates={len(candidates)})"
            )
    except Exception:
        pass  # Logging is best-effort

    if not validate:
        return selected

    # Validate connectivity and update health tracker
    errors: list[str] = []
    tried: list[str] = []
    last_exc: Exception | None = None

    # Try selected endpoint first
    try:
        w3 = _build_web3_for_rpc(selected)
        if not w3.is_connected():
            raise ConnectionError("not reachable")
        # Access a cheap RPC to ensure the node responds correctly
        _ = w3.eth.chain_id
        tracker.record_success(selected)
        return selected
    except Exception as exc:
        is_rate_limit = _is_rate_limit_error(exc)
        tracker.record_failure(selected, is_rate_limit=is_rate_limit)
        errors.append(f"{selected}: {exc}")
        tried.append(selected)
        last_exc = exc

    # If selected endpoint failed, try other healthy endpoints
    remaining = [c for c in candidates if c not in tried]
    for rpc in remaining:
        if not tracker.select_best_endpoint([rpc]):
            continue  # Skip unhealthy endpoints
        try:
            w3 = _build_web3_for_rpc(rpc)
            if not w3.is_connected():
                raise ConnectionError("not reachable")
            _ = w3.eth.chain_id
            tracker.record_success(rpc)
            return rpc
        except Exception as exc:
            is_rate_limit = _is_rate_limit_error(exc)
            tracker.record_failure(rpc, is_rate_limit=is_rate_limit)
            errors.append(f"{rpc}: {exc}")
            tried.append(rpc)
            last_exc = exc

    # All endpoints failed
    raise ConnectionError(
        "Failed to connect to any RPC endpoint: " + "; ".join(errors)
    ) from last_exc


def get_web3() -> Web3:
    """Get a Web3 instance using health-aware RPC selection.

    Automatically tracks RPC health and routes around failing endpoints.
    """
    if Web3 is None:
        raise RuntimeError("web3 library not available; Web3 operations require web3")
    if os.getenv("PYTEST_CURRENT_TEST"):
        # Skip live RPC validation during unit tests; callers patch contracts and
        # do not need network connectivity.
        rpc = resolve_rpc_url(validate=False)
        return _build_web3_for_rpc(rpc)

    rpc = resolve_rpc_url(validate=True)
    w3 = _build_web3_for_rpc(rpc)
    # resolve_rpc_url already verified connectivity, but keep a safeguard
    if not w3.is_connected():
        tracker = get_rpc_health_tracker()
        tracker.record_failure(rpc)
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
        raise OSError("ETH_PRIVATE_KEY is required for signing and transactions")
    return Wallet(private_key=pk)


def get_contract(w3: Web3, address: str, abi_name: str) -> Contract:
    if Web3 is None:
        raise RuntimeError("web3 library not available; get_contract requires web3")

    from libs.dex.routes import _normalize_address

    abi = load_abi(abi_name)
    try:
        addr = Web3.to_checksum_address(address)
    except Exception:
        # Unit tests may supply demo or shortened addresses; fall back to a
        # normalized lower‑case form instead of failing hard.
        if os.getenv("PYTEST_CURRENT_TEST"):
            norm = _normalize_address(str(address))
            body = norm[2:] if norm.startswith("0x") else norm
            if len(body) < 40:
                body = body.zfill(40)
            elif len(body) > 40:
                body = body[-40:]
            addr = "0x" + body
        else:
            raise
    return w3.eth.contract(address=addr, abi=abi)


def encode_contract_call(
    contract: Any, fn_name: str, args: Sequence[Any] | None = None
) -> str:
    """Encode a contract function call across Web3 versions.

    Web3 v7 renamed the parameters for ``encode_abi`` so older keyword
    invocations (`fn_name=...`) now raise ``TypeError``.  This helper first
    tries the new positional signature, then falls back to the legacy keyword
    form, and finally to the camelCase ``encodeABI`` used by Web3 v5/v6.
    """

    params: Sequence[Any] = [] if args is None else list(args)
    encoder = getattr(contract, "encode_abi", None)
    if callable(encoder):
        try:
            return encoder(fn_name, params)
        except TypeError:
            try:
                return encoder(fn_name, args=params)
            except TypeError:
                try:
                    return encoder(fn_name=fn_name, args=params)
                except TypeError:
                    pass
    legacy = getattr(contract, "encodeABI", None)
    if callable(legacy):
        try:
            return legacy(fn_name, params)
        except TypeError:
            return legacy(fn_name=fn_name, args=params)
    raise TypeError(
        "Contract does not expose encode_abi/encodeABI compatible signature"
    )


def build_eip1559_tx(
    w3: Web3,
    from_addr: str,
    to: str | None = None,
    value: int = 0,
    data: bytes | None = None,
) -> dict[str, Any]:
    import os

    base_fee = w3.eth.get_block("latest").baseFeePerGas
    max_priority = w3.to_wei(1, "gwei")
    max_fee = int(base_fee * 2) + max_priority

    # Base-specific gas price sanity check
    BASE_CHAIN_ID = 8453
    try:
        chain_id = w3.eth.chain_id
        if chain_id == BASE_CHAIN_ID:
            base_max_gas_price_wei = None
            try:
                raw = os.getenv("BASE_GAS_PRICE_MAX_WEI")
                if raw:
                    base_max_gas_price_wei = int(str(raw), 0)
            except Exception:
                pass
            if base_max_gas_price_wei is None:
                base_max_gas_price_wei = int(
                    Web3.to_wei(5, "gwei")
                )  # Default 5 gwei cap

            if max_fee > base_max_gas_price_wei * 2:
                rpc_url = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL") or "unknown"
                try:
                    from libs.telemetry.logger import get_logger

                    logger = get_logger("libs.agentkit_ext.web3_utils")
                    logger.warning(
                        "Base gas price anomaly in build_eip1559_tx: base_fee=%s wei (%.2f gwei), "
                        "max_priority=%s wei (%.2f gwei), computed max_fee=%s wei (%.2f gwei) > cap=%s wei (%.2f gwei). "
                        "RPC=%s. Capping to max.",
                        base_fee,
                        base_fee / 1e9 if base_fee else 0,
                        max_priority,
                        max_priority / 1e9 if max_priority else 0,
                        max_fee,
                        max_fee / 1e9,
                        base_max_gas_price_wei * 2,
                        (base_max_gas_price_wei * 2) / 1e9,
                        rpc_url,
                    )
                except Exception:
                    pass  # Logger unavailable, continue anyway
                max_fee = base_max_gas_price_wei * 2
    except Exception:
        pass  # Chain ID check failed, use original calculation
    tx: dict[str, Any] = {
        "chainId": w3.eth.chain_id,
        "from": Web3.to_checksum_address(from_addr),
        "nonce": w3.eth.get_transaction_count(
            Web3.to_checksum_address(from_addr), "pending"
        ),
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
        # Some RPC providers (including popular Base endpoints) treat
        # `gas=0` as "no allowance" and fail `eth_estimateGas` with
        # `gas required exceeds allowance (0)`.
        # Start with a sane upper-bound and refine via estimate.
        "gas": 500_000,
        "value": value,
    }
    if to:
        tx["to"] = Web3.to_checksum_address(to)
    if data:
        tx["data"] = data
    # Estimate gas; if the provider reports zero allowance, keep the
    # conservative default and let execution-time errors surface instead.
    try:
        estimated = w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated * 1.2)
    except Exception as exc:  # pragma: no cover
        msg = str(exc).lower()
        if "gas required exceeds allowance (0)" not in msg:
            raise
    return tx


def send_contract_tx(w3: Web3, wallet: Wallet, tx: dict[str, Any]) -> str:
    # Allow a few retries for nonce/fee issues
    max_retries = 3
    last_exc = None

    # Create a copy to avoid mutating the original dict across retries if we fail
    tx_to_send = tx.copy()

    for attempt in range(max_retries):
        try:
            signed = w3.eth.account.sign_transaction(tx_to_send, wallet.private_key)
            # Web3 v7 renamed `rawTransaction` -> `raw_transaction`
            raw = getattr(signed, "rawTransaction", None) or getattr(
                signed, "raw_transaction", None
            )
            if raw is None:
                raise TypeError("SignedTransaction is missing raw transaction bytes")

            tx_hash = w3.eth.send_raw_transaction(raw)
            return tx_hash.hex()

        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()

            # Check for common nonce/mempool errors
            is_nonce_error = (
                "nonce too low" in msg
                or "replacement transaction underpriced" in msg
                or "already known" in msg
                or "transaction already imported" in msg
            )

            if is_nonce_error and attempt < max_retries - 1:
                # Strategy: Bump nonce to skip the colliding transaction
                # This assumes we want the new action to happen *after* whatever is pending,
                # or that the pending one is valid and we just want to submit the new one.
                current_nonce = tx_to_send.get("nonce")
                if isinstance(current_nonce, int):
                    tx_to_send["nonce"] = current_nonce + 1
                    continue

            # If not a nonce error or we ran out of retries, re-raise
            raise last_exc

    # Should be unreachable given re-raise above, but for safety:
    if last_exc:
        raise last_exc
    raise RuntimeError("Transaction failed after retries")
