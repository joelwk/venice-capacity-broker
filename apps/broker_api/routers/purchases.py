from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from db.session import create_db_and_tables, get_session

try:
    # Optional helper to bootstrap tables on first request if migrations weren't run.
    from db.session import create_all_unconditional  # type: ignore
except Exception:  # pragma: no cover - fallback when helper is absent
    create_all_unconditional = None  # type: ignore
from db.models import Purchase, Quote

try:
    from sqlmodel import select as _select  # type: ignore

    _SQLMODEL_AVAILABLE = True
except ModuleNotFoundError:
    _select = None  # type: ignore[assignment]
    _SQLMODEL_AVAILABLE = False

try:
    from sqlalchemy.exc import IntegrityError  # type: ignore
except Exception:  # pragma: no cover - optional dependency

    class IntegrityError(Exception):  # type: ignore[no-redef]
        pass


try:
    from eth_account import Account  # type: ignore
    from eth_account.messages import encode_defunct  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Account = None  # type: ignore
    encode_defunct = None  # type: ignore

from services.venice_keys.manager import KeyManager

from ..models import (
    PurchaseRecoverChallenge,
    PurchaseRecoverRequest,
    PurchaseStatus,
    PurchaseVerifyRequest,
)
from ..public_rate_limit import enforce_public_rate_limit
from ..tenant_store import Tenant, TenantStore

router = APIRouter()

_store: TenantStore
_keys: KeyManager
_logger: logging.Logger = logging.getLogger("broker.api.purchases")
_extract_field: Callable[[dict, tuple[str, ...]], str]
_purchases_enabled: bool

_CHALLENGE_TTL_SECONDS = 600
_MAX_CHALLENGES = 500
_challenge_store: dict[str, dict] = {}
_STREAM_MAX_SECONDS = 300

# Payment acceptance policy. Base mainnet finalizes quickly (~2s blocks), so a
# handful of confirmations meaningfully reduces reorg risk without hurting UX.
_DEFAULT_MIN_CONFIRMATIONS = 5
_DEFAULT_CHAIN_ID = 8453  # Base mainnet
_DEFAULT_UNDERPAY_TOLERANCE_BPS = 0


def _stream_max_seconds() -> float:
    raw = os.getenv("PURCHASE_STREAM_MAX_SECONDS") or ""
    if not raw:
        return float(_STREAM_MAX_SECONDS)
    try:
        value = float(raw)
    except Exception:
        return float(_STREAM_MAX_SECONDS)
    return value if value > 0 else float(_STREAM_MAX_SECONDS)


def init_router(
    *,
    store: TenantStore,
    keys: KeyManager,
    logger: logging.Logger,
    extract_field: Callable[[dict, tuple[str, ...]], str],
    purchases_enabled: bool,
) -> APIRouter:
    global _store, _keys, _logger, _extract_field, _purchases_enabled

    _store = store
    _keys = keys
    _logger = logger
    _extract_field = extract_field
    _purchases_enabled = bool(purchases_enabled)

    if _purchases_enabled and not _SQLMODEL_AVAILABLE:
        _purchases_enabled = False
        try:  # pragma: no cover - logging only
            _logger.warning(
                "purchases: disabling router because sqlmodel is not installed"
            )
        except Exception:
            pass
        return router

    if not _purchases_enabled:
        return router

    try:
        create_db_and_tables()
    except Exception as exc:  # pragma: no cover - optional dependency path
        try:
            _logger.warning("purchases: skipping db init (%s)", exc)
        except Exception:
            pass
    return router


def _normalize_hex(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not value.startswith("0x"):
        value = "0x" + value
    return value.lower()


def _wei(hex_str: str) -> int:
    return int(hex_str, 16)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _topic_to_address(topic: str) -> str:
    """Extract the 20-byte address from a 32-byte-padded log topic."""
    normalized = _normalize_hex(topic)
    if len(normalized) <= 42:
        return normalized
    return "0x" + normalized[-40:]


def _cleanup_challenges() -> None:
    """Remove expired challenges and keep the store bounded."""
    now = datetime.utcnow()
    expired = [
        nonce
        for nonce, data in _challenge_store.items()
        if data.get("expires_at") <= now
    ]
    for nonce in expired:
        _challenge_store.pop(nonce, None)
    # Prevent unbounded growth by pruning oldest entries
    if len(_challenge_store) > _MAX_CHALLENGES:
        sorted_items = sorted(
            _challenge_store.items(), key=lambda item: item[1].get("created_at", now)
        )
        for nonce, _ in sorted_items[: len(_challenge_store) - _MAX_CHALLENGES]:
            _challenge_store.pop(nonce, None)


def _build_challenge_message(
    *, tx_hash: str, buyer_addr: str, nonce: str, expires_at: datetime
) -> str:
    expires_iso = expires_at.replace(microsecond=0).isoformat() + "Z"
    return (
        "Venice Capacity Broker Wallet Verification\n"
        f"Transaction: {tx_hash}\n"
        f"Buyer: {buyer_addr}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_iso}"
    )


def _create_challenge(tx_hash: str, buyer_addr: str) -> dict:
    _cleanup_challenges()
    nonce = secrets.token_hex(16)
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=_CHALLENGE_TTL_SECONDS)
    message = _build_challenge_message(
        tx_hash=tx_hash, buyer_addr=buyer_addr, nonce=nonce, expires_at=expires_at
    )
    payload = {
        "tx_hash": tx_hash,
        "buyer": buyer_addr,
        "message": message,
        "nonce": nonce,
        "created_at": now,
        "expires_at": expires_at,
    }
    _challenge_store[nonce] = payload
    return payload


def _pop_challenge(nonce: str) -> dict | None:
    _cleanup_challenges()
    return _challenge_store.pop(nonce, None)


def _rpc_timeout_seconds() -> float:
    raw = os.getenv("RPC_REQUEST_TIMEOUT_SECONDS") or os.getenv(
        "BASE_RPC_TIMEOUT_SECONDS"
    )
    try:
        return float(raw) if raw is not None else 15.0
    except Exception:
        return 15.0


def _resolve_rpc_urls() -> list[str]:
    try:
        from libs.runtime.rpc_validation import get_rpc_urls_from_env

        urls = get_rpc_urls_from_env()
    except Exception:
        urls = []
        for key in ("RPC_URLS", "BASE_RPC_URLS"):
            val = os.getenv(key)
            if val:
                urls.extend([part.strip() for part in val.replace(",", " ").split()])
        for key in ("RPC_URL", "BASE_RPC_URL"):
            val = (os.getenv(key) or "").strip()
            if val:
                urls.append(val)
        for key in ("RPC_URL_FALLBACK", "BASE_RPC_URL_FALLBACK"):
            val = (os.getenv(key) or "").strip()
            if val:
                urls.extend([part.strip() for part in val.replace(",", " ").split()])
    seen = set()
    ordered: list[str] = []
    for url in urls:
        if url and url not in seen:
            ordered.append(url)
            seen.add(url)
    return ordered


def _rpc_call(urls: list[str], method: str, params: list) -> dict:
    import requests

    if not urls:
        raise RuntimeError("No RPC URLs configured")

    timeout = _rpc_timeout_seconds()
    last_exc: Exception | None = None

    def _call(url: str) -> dict:
        response = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(str(payload["error"]))
        return payload["result"]

    max_workers = max(1, len(urls))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_call, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                last_exc = exc
                try:
                    _logger.debug("purchase verify: rpc failed on %s: %s", url, exc)
                except Exception:
                    pass
                continue
            for pending in futures:
                if pending is not future:
                    pending.cancel()
            return result

    raise RuntimeError("RPC call failed") from last_exc


@router.get(
    "/v1/purchases/challenge",
    response_model=PurchaseRecoverChallenge,
    dependencies=[Depends(enforce_public_rate_limit)],
)
@router.get(
    "/v1/purchases/recover/challenge",
    response_model=PurchaseRecoverChallenge,
    dependencies=[Depends(enforce_public_rate_limit)],
)
def purchases_recover_challenge(txHash: str, buyerAddress: str) -> dict:
    """Issue a single-use signing challenge binding a tx hash to a wallet.

    Used both for initial purchase verification and key recovery.
    """
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")
    if Account is None or encode_defunct is None:
        raise HTTPException(
            status_code=503, detail="signature verification unavailable"
        )

    tx_hash_norm = _normalize_hex(txHash)
    buyer_norm = _normalize_hex(buyerAddress)
    if not tx_hash_norm or len(tx_hash_norm) != 66:
        raise HTTPException(status_code=400, detail="txHash invalid")
    if not buyer_norm or len(buyer_norm) != 42:
        raise HTTPException(status_code=400, detail="buyerAddress invalid")

    challenge = _create_challenge(tx_hash_norm, buyer_norm)
    expires_iso = challenge["expires_at"].replace(microsecond=0).isoformat() + "Z"
    return {
        "message": challenge["message"],
        "nonce": challenge["nonce"],
        "expiresAt": expires_iso,
        "txHash": tx_hash_norm,
        "buyerAddress": buyer_norm,
    }


def _verify_wallet_signature(
    tx_hash_norm: str, buyer_norm: str, signature: str | None, nonce: str | None
) -> None:
    """Require proof of wallet ownership before any key is issued or returned.

    Both tx hash and sender address are public on-chain, so without this gate
    anyone watching the treasury could claim keys for other people's payments.
    """
    if Account is None or encode_defunct is None:
        raise HTTPException(
            status_code=503, detail="signature verification unavailable"
        )
    if not signature or not nonce:
        raise HTTPException(
            status_code=401,
            detail=(
                "wallet signature required: request a challenge via "
                "/v1/purchases/challenge and sign it with the paying wallet"
            ),
        )

    challenge = _pop_challenge(str(nonce))
    if not challenge:
        raise HTTPException(status_code=400, detail="challenge expired or invalid")

    expires_at = challenge.get("expires_at")
    if not isinstance(expires_at, datetime) or expires_at <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="challenge expired")

    if challenge.get("tx_hash") != tx_hash_norm or challenge.get("buyer") != buyer_norm:
        raise HTTPException(status_code=400, detail="challenge mismatch")

    message = str(challenge.get("message") or "")
    if not message:
        raise HTTPException(status_code=400, detail="challenge message missing")

    try:
        msg = encode_defunct(text=message)
        recovered_addr = Account.recover_message(msg, signature=str(signature))
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid signature: {exc}"
        ) from exc

    if _normalize_hex(recovered_addr) != buyer_norm:
        raise HTTPException(
            status_code=403, detail="signature does not match buyer address"
        )


@router.post(
    "/v1/purchases/recover",
    response_model=PurchaseStatus,
    dependencies=[Depends(enforce_public_rate_limit)],
)
def purchases_recover(req: PurchaseRecoverRequest) -> dict:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")

    tx_hash_norm = _normalize_hex(req.txHash)
    buyer_norm = _normalize_hex(req.buyerAddress)
    if not tx_hash_norm or len(tx_hash_norm) != 66:
        raise HTTPException(status_code=400, detail="txHash invalid")
    if not buyer_norm or len(buyer_norm) != 42:
        raise HTTPException(status_code=400, detail="buyerAddress invalid")

    _verify_wallet_signature(tx_hash_norm, buyer_norm, req.signature, req.nonce)

    purchase_id = hashlib.sha256(f"{tx_hash_norm}:{buyer_norm}".encode()).hexdigest()[
        :16
    ]
    raw_purchase_id = hashlib.sha256(
        f"{req.txHash}:{req.buyerAddress}".encode()
    ).hexdigest()[:16]
    with next(get_session()) as session:  # type: ignore[call-arg]
        try:
            purchase = session.exec(
                _select(Purchase).where(Purchase.purchase_id == purchase_id)
            ).first()  # type: ignore[misc]
            if purchase is None and raw_purchase_id != purchase_id:
                purchase = session.exec(
                    _select(Purchase).where(Purchase.purchase_id == raw_purchase_id)
                ).first()  # type: ignore[misc]
            if purchase is None:
                purchase = session.exec(
                    _select(Purchase)
                    .where(Purchase.tx_hash == tx_hash_norm)
                    .where(Purchase.buyer_address == buyer_norm)
                ).first()  # type: ignore[misc]
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            is_table_error = (
                "UndefinedTable" in text
                or 'relation "purchase" does not exist' in text.lower()
                or 'relation "quote" does not exist' in text.lower()
                or "InFailedSqlTransaction" in text
                or "current transaction is aborted" in text.lower()
            )
            if is_table_error and callable(create_all_unconditional):
                try:
                    session.rollback()
                except Exception:
                    pass
                try:
                    create_all_unconditional()  # type: ignore[misc]
                except Exception:
                    pass
                try:
                    purchase = session.exec(
                        _select(Purchase).where(Purchase.purchase_id == purchase_id)
                    ).first()  # type: ignore[misc]
                    if purchase is None and raw_purchase_id != purchase_id:
                        purchase = session.exec(
                            _select(Purchase).where(
                                Purchase.purchase_id == raw_purchase_id
                            )
                        ).first()  # type: ignore[misc]
                    if purchase is None:
                        purchase = session.exec(
                            _select(Purchase)
                            .where(Purchase.tx_hash == tx_hash_norm)
                            .where(Purchase.buyer_address == buyer_norm)
                        ).first()  # type: ignore[misc]
                except Exception as retry_exc:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    raise retry_exc
            else:
                raise

        if purchase is None:
            raise HTTPException(status_code=404, detail="purchase not found")

        if _normalize_hex(purchase.buyer_address) != buyer_norm:
            raise HTTPException(status_code=403, detail="wallet address mismatch")
        if _normalize_hex(purchase.tx_hash) != tx_hash_norm:
            raise HTTPException(status_code=403, detail="transaction hash mismatch")

        quote_id = purchase.quote_id
        tenant_id = purchase.tenant_id
        if not quote_id:
            raise HTTPException(status_code=404, detail="associated quote not found")

    verify_req = PurchaseVerifyRequest(
        quoteId=quote_id,
        txHash=req.txHash,
        buyerAddress=req.buyerAddress,
        tenantId=tenant_id or None,
    )
    # Wallet ownership was proven above; skip the second signature gate.
    return _execute_verified_purchase(verify_req)


_PAYMENT_ASSETS = ("USDC", "ETH", "WBTC")
_BASE_WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
_BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _env_address(*names: str) -> str | None:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def _asset_to_token_address(asset: str) -> str | None:
    """Map asset symbol (ETH, USDC, WBTC) to token address."""
    asset_upper = asset.upper().strip()
    if asset_upper == "ETH":
        return (
            _env_address("WETH_ADDRESS", "WETH_TOKEN_ADDRESS") or _BASE_WETH_ADDRESS
        )
    if asset_upper == "USDC":
        return (
            _env_address("USDC_ADDRESS", "QUOTE_TOKEN_ADDRESS") or _BASE_USDC_ADDRESS
        )
    if asset_upper == "WBTC":
        # Market data, .env.example, and Replit templates use WBTC_TOKEN_ADDRESS.
        # Keep WBTC_ADDRESS as a legacy alias so older secrets still verify.
        return _env_address("WBTC_TOKEN_ADDRESS", "WBTC_ADDRESS")
    return None


def payment_asset_supported(asset: str) -> bool:
    """True when payments in this asset can be verified on-chain."""
    return _asset_to_token_address(asset or "") is not None


def configured_payment_assets() -> list[str]:
    """Payment assets the buy page may offer given current env."""
    return [asset for asset in _PAYMENT_ASSETS if payment_asset_supported(asset)]


def _verify_tx(
    quote: Quote,
    tx_hash: str,
    treasury: str,
    expected_token_addr: str | None,
    rpc_urls: list[str],
    buyer_address: str,
) -> tuple[int, dict]:
    tx_hash = _normalize_hex(tx_hash)
    buyer_address = _normalize_hex(buyer_address)
    if not tx_hash or len(tx_hash) != 66:
        raise RuntimeError("transaction hash required")
    if not buyer_address or len(buyer_address) != 42:
        raise RuntimeError("buyer address invalid")
    treasury_norm = _normalize_hex(treasury)
    expected_token_norm = (
        _normalize_hex(expected_token_addr) if expected_token_addr else None
    )
    expected_chain_id = _env_int(
        "PURCHASE_CHAIN_ID", _env_int("BASE_CHAIN_ID", _DEFAULT_CHAIN_ID)
    )
    if expected_chain_id > 0:
        chain_id_hex = _rpc_call(rpc_urls, "eth_chainId", [])
        actual_chain_id = _wei(str(chain_id_hex))
        if actual_chain_id != expected_chain_id:
            raise RuntimeError(
                f"wrong chain: rpc reports chain id {actual_chain_id}, "
                f"expected {expected_chain_id}"
            )

    receipt = _rpc_call(rpc_urls, "eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        raise RuntimeError("transaction not found")
    status_hex = receipt.get("status") or "0x0"
    if _wei(status_hex) != 1:
        raise RuntimeError("transaction failed")

    min_confirmations = _env_int(
        "PURCHASE_MIN_CONFIRMATIONS", _DEFAULT_MIN_CONFIRMATIONS
    )
    if min_confirmations > 0:
        receipt_block_hex = receipt.get("blockNumber")
        if not receipt_block_hex:
            raise RuntimeError("transaction pending: not yet included in a block")
        current_block_hex = _rpc_call(rpc_urls, "eth_blockNumber", [])
        confirmations = _wei(str(current_block_hex)) - _wei(str(receipt_block_hex)) + 1
        if confirmations < min_confirmations:
            raise RuntimeError(
                f"transaction has {confirmations}/{min_confirmations} confirmations; "
                "retry once the payment is finalized"
            )

    logs = receipt.get("logs") or []
    total_paid = 0
    verification: dict[str, dict] = {}
    transfer_events_found = 0
    treasury_transfers_found = 0
    token_addresses_seen = set()

    for idx, log in enumerate(logs):
        topics = [str(t).lower() for t in log.get("topics") or []]
        if len(topics) < 3:
            continue
        transfer_sig = (
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )
        if topics[0] != transfer_sig:
            continue
        transfer_events_found += 1
        from_addr = _topic_to_address(topics[1])
        to_addr = _topic_to_address(topics[2])
        token_addr = _normalize_hex(log.get("address") or "")
        if token_addr:
            token_addresses_seen.add(token_addr)
        if to_addr != treasury_norm:
            continue
        # Require the transfer to originate from the declared buyer wallet.
        # This prevents third-party relays from spoofing payments.
        if from_addr != buyer_address:
            continue
        treasury_transfers_found += 1
        if expected_token_norm and token_addr != expected_token_norm:
            continue
        data_hex = str(log.get("data") or "0x0")
        amount = _wei(data_hex)
        total_paid += amount
        verification[str(idx)] = {
            "from": from_addr,
            "to": to_addr,
            "amount": amount,
            "token": token_addr,
        }
    # If no ERC-20 transfer matched and asset is ETH, fall back to native value
    # transfers (simple ETH send has no Transfer logs).
    if total_paid <= 0 and (quote.asset or "").strip().upper() == "ETH":
        tx = _rpc_call(rpc_urls, "eth_getTransactionByHash", [tx_hash])
        if tx is None:
            raise RuntimeError("transaction not found")
        tx_from = _normalize_hex(tx.get("from") or "")
        tx_to = _normalize_hex(tx.get("to") or "")
        if tx_from != buyer_address:
            raise RuntimeError(f"unexpected address: from={tx_from}")
        if tx_to != treasury_norm:
            raise RuntimeError(f"unexpected treasury: to={tx_to}")
        value_hex = str(tx.get("value") or "0x0")
        value = _wei(value_hex)
        if value > 0:
            total_paid = value
            verification["native"] = {
                "from": tx_from,
                "to": tx_to,
                "amount": value,
                "asset": "ETH",
            }

    if total_paid <= 0:
        # Provide diagnostic information in the error message and log
        error_details = [
            f"treasury={treasury_norm}",
            f"transfer_events={transfer_events_found}",
            f"treasury_transfers={treasury_transfers_found}",
            f"token_filter={'enabled' if expected_token_norm else 'disabled'}",
        ]
        if expected_token_norm:
            error_details.append(f"expected_token={expected_token_norm}")
            error_details.append(f"quote_asset={quote.asset}")
        if token_addresses_seen:
            error_details.append(f"tokens_seen={len(token_addresses_seen)}")
            # Log first few token addresses for debugging
            token_list = list(token_addresses_seen)[:5]
            error_details.append(f"token_addrs={','.join(token_list)}")
        error_msg = f"no treasury transfer found ({', '.join(error_details)})"
        try:
            _logger.warning("purchase verify: %s tx=%s", error_msg, tx_hash)
        except Exception:
            pass  # Logging is best-effort
        raise RuntimeError(error_msg)
    return total_paid, verification


@router.post(
    "/v1/purchases/verify",
    response_model=PurchaseStatus,
    dependencies=[Depends(enforce_public_rate_limit)],
)
def verify_purchase(req: PurchaseVerifyRequest) -> dict:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")

    tx_hash_norm = _normalize_hex(req.txHash)
    if not tx_hash_norm or len(tx_hash_norm) != 66:
        raise HTTPException(status_code=400, detail="txHash invalid")
    buyer_norm = _normalize_hex(req.buyerAddress)
    if not buyer_norm or len(buyer_norm) != 42:
        raise HTTPException(status_code=400, detail="buyerAddress invalid")

    _verify_wallet_signature(tx_hash_norm, buyer_norm, req.signature, req.nonce)
    return _execute_verified_purchase(req)


def _execute_verified_purchase(req: PurchaseVerifyRequest) -> dict:
    """Verify the payment on-chain and issue the scoped Venice key.

    Callers must have already proven ownership of the buyer wallet.
    """
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")

    rpc_urls = _resolve_rpc_urls()
    treasury = (_get_env("TREASURY_ADDRESS") or "").strip()
    if not treasury:
        raise HTTPException(status_code=400, detail="TREASURY_ADDRESS not set")
    if not rpc_urls:
        raise HTTPException(
            status_code=400,
            detail="BASE_RPC_URL or BASE_RPC_URLS not set",
        )

    buyer_norm = _normalize_hex(req.buyerAddress)
    if not buyer_norm or len(buyer_norm) != 42:
        raise HTTPException(status_code=400, detail="buyerAddress invalid")

    tx_hash_norm = _normalize_hex(req.txHash)
    if not tx_hash_norm or len(tx_hash_norm) != 66:
        raise HTTPException(status_code=400, detail="txHash invalid")

    allowlist_raw = _get_env("PURCHASE_TENANT_ALLOWLIST") or ""
    tenant_allow = {
        item.strip().lower() for item in allowlist_raw.split(",") if item.strip()
    }
    default_tenant = "w:" + buyer_norm.lower()

    with next(get_session()) as session:  # type: ignore[call-arg]
        pur_id = hashlib.sha256(f"{tx_hash_norm}:{buyer_norm}".encode()).hexdigest()[
            :16
        ]
        raw_id = hashlib.sha256(
            f"{req.txHash}:{req.buyerAddress}".encode()
        ).hexdigest()[:16]
        try:
            existing = session.exec(
                _select(Purchase).where(Purchase.purchase_id == pur_id)
            ).first()  # type: ignore[misc]
            if existing is None and raw_id != pur_id:
                existing = session.exec(
                    _select(Purchase).where(Purchase.purchase_id == raw_id)
                ).first()  # type: ignore[misc]
            if existing is None:
                existing = session.exec(
                    _select(Purchase)
                    .where(Purchase.tx_hash == tx_hash_norm)
                    .where(Purchase.buyer_address == buyer_norm)
                ).first()  # type: ignore[misc]
        except Exception as exc:
            # Handle missing tables in fresh deployments (UndefinedTable / relation does not exist).
            # Also handle InFailedSqlTransaction which occurs when a transaction is aborted.
            text = f"{type(exc).__name__}: {exc}"
            is_table_error = (
                "UndefinedTable" in text
                or 'relation "purchase" does not exist' in text.lower()
                or 'relation "quote" does not exist' in text.lower()
                or "InFailedSqlTransaction" in text
                or "current transaction is aborted" in text.lower()
            )
            if is_table_error and callable(create_all_unconditional):
                # Rollback the failed transaction before retrying
                try:
                    session.rollback()
                except Exception:
                    pass
                try:
                    create_all_unconditional()  # type: ignore[misc]
                except Exception:
                    pass
                # Re-run the lookup after best-effort bootstrap and rollback
                try:
                    existing = session.exec(
                        _select(Purchase).where(Purchase.purchase_id == pur_id)
                    ).first()  # type: ignore[misc]
                    if existing is None and raw_id != pur_id:
                        existing = session.exec(
                            _select(Purchase).where(Purchase.purchase_id == raw_id)
                        ).first()  # type: ignore[misc]
                    if existing is None:
                        existing = session.exec(
                            _select(Purchase)
                            .where(Purchase.tx_hash == tx_hash_norm)
                            .where(Purchase.buyer_address == buyer_norm)
                        ).first()  # type: ignore[misc]
                except Exception as retry_exc:
                    # If retry still fails, rollback and raise
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    raise retry_exc
            else:
                raise
        if existing is None and (
            req.txHash != tx_hash_norm or req.buyerAddress != buyer_norm
        ):
            alt_id = hashlib.sha256(
                f"{tx_hash_norm}:{buyer_norm}".encode()
            ).hexdigest()[:16]
            existing = session.exec(
                _select(Purchase).where(Purchase.purchase_id == alt_id)
            ).first()  # type: ignore[misc]
            if existing is not None:
                pur_id = existing.purchase_id

        quote_id = req.quoteId
        if existing and existing.quote_id:
            if req.quoteId and existing.quote_id != req.quoteId:
                raise HTTPException(
                    status_code=409,
                    detail="purchase already recorded for a different quote",
                )
            quote_id = existing.quote_id

        try:
            quote = session.exec(
                _select(Quote).where(Quote.quote_id == quote_id)
            ).first()  # type: ignore[misc]
        except Exception as exc:
            # Handle missing tables in fresh deployments (UndefinedTable / relation does not exist).
            # Also handle InFailedSqlTransaction which occurs when a transaction is aborted.
            text = f"{type(exc).__name__}: {exc}"
            is_table_error = (
                "UndefinedTable" in text
                or 'relation "quote" does not exist' in text.lower()
                or "InFailedSqlTransaction" in text
                or "current transaction is aborted" in text.lower()
            )
            if is_table_error and callable(create_all_unconditional):
                # Rollback the failed transaction before retrying
                try:
                    session.rollback()
                except Exception:
                    pass
                try:
                    create_all_unconditional()  # type: ignore[misc]
                except Exception:
                    pass
                # Re-run the lookup after best-effort bootstrap and rollback
                try:
                    quote = session.exec(
                        _select(Quote).where(Quote.quote_id == quote_id)
                    ).first()  # type: ignore[misc]
                except Exception as retry_exc:
                    # If retry still fails, rollback and raise
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    raise retry_exc
            else:
                raise
        if quote is None:
            raise HTTPException(status_code=404, detail="quote not found")

        if existing and existing.status == "fulfilled" and existing.subkey:
            expires_iso = (
                existing.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if existing.expires_at
                else None
            )
            return {
                "purchaseId": existing.purchase_id,
                "status": existing.status,
                "tenantId": existing.tenant_id,
                "subkey": existing.subkey,
                "expiresAt": expires_iso,
            }

        now = datetime.utcnow()
        if quote.expires_at and quote.expires_at < now and existing is None:
            raise HTTPException(status_code=400, detail="quote expired")
        if (quote.status or "open") not in {"open"} and existing is None:
            raise HTTPException(
                status_code=409, detail=f"quote not open (status={quote.status})"
            )

        # Fail closed: without a token contract binding, any ERC-20 transfer
        # to the treasury would satisfy verification.
        expected_token_addr = _asset_to_token_address(quote.asset)
        if expected_token_addr is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"asset {quote.asset} not supported: "
                    "payment token address not configured"
                ),
            )

        try:
            paid_val, receipt = _verify_tx(
                quote, tx_hash_norm, treasury, expected_token_addr, rpc_urls, buyer_norm
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        amount_paid = int(paid_val)
        required_payment = int(quote.total_price)
        tolerance_bps = _env_int(
            "PURCHASE_UNDERPAY_TOLERANCE_BPS", _DEFAULT_UNDERPAY_TOLERANCE_BPS
        )
        min_required = required_payment * (10_000 - tolerance_bps) // 10_000
        if amount_paid < min_required:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"insufficient payment: paid {amount_paid} of required "
                    f"{required_payment} {quote.asset} base units"
                ),
            )

        if existing is None:
            purchase = Purchase(
                purchase_id=pur_id,
                quote_id=quote.quote_id,
                buyer_address=buyer_norm,
                asset=quote.asset,
                amount_paid=amount_paid,
                tx_hash=tx_hash_norm,
                status="confirmed",
            )
            session.add(purchase)
        else:
            purchase = existing
            purchase.quote_id = quote.quote_id
            purchase.buyer_address = buyer_norm
            purchase.asset = quote.asset
            purchase.amount_paid = amount_paid
            purchase.tx_hash = tx_hash_norm
            purchase.status = "confirmed"

        try:
            purchase.receipt = json.dumps(
                {
                    "txHash": tx_hash_norm,
                    "network": (_get_env("NETWORK_ID") or "base-mainnet"),
                    "asset": quote.asset,
                    "amountPaid": amount_paid,
                    "quote": {
                        "quoteId": quote.quote_id,
                        "units": float(quote.units),
                        "unitPrice": int(quote.unit_price),
                        "totalPrice": int(quote.total_price),
                    },
                    "verification": receipt,
                    "verifiedAt": int(time.time()),
                }
            )
        except Exception:
            pass

        tenant_id = default_tenant
        if req.tenantId:
            requested_id = req.tenantId.strip()
            if (
                requested_id.lower() == default_tenant
                or requested_id.lower() in tenant_allow
            ):
                tenant_id = requested_id
            else:
                raise HTTPException(
                    status_code=403, detail="tenantId override not permitted"
                )

        existing_tenant = _store.get(tenant_id)
        if (
            existing_tenant
            and existing_tenant.owner_address
            and existing_tenant.owner_address.lower() != buyer_norm
        ):
            raise HTTPException(
                status_code=409, detail="tenant already assigned to a different wallet"
            )
        if (
            existing_tenant
            and existing_tenant.owner_address is None
            and tenant_id != default_tenant
        ):
            raise HTTPException(
                status_code=409, detail="tenant reserved for administrator"
            )

        limit_kind = (_get_env("PURCHASE_UNITS_KIND") or "diem").strip().lower()
        try:
            units_value = float(quote.units)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="quote units invalid") from exc
        if units_value <= 0:
            raise HTTPException(status_code=400, detail="quote units must be positive")
        if limit_kind == "diem":
            consumption = {"diem": round(units_value, 12)}
        else:
            consumption = {limit_kind: max(1, int(math.ceil(units_value)))}

        # Claim the transaction before minting: the unique tx_hash constraint
        # serializes concurrent verifies so one payment yields one key.
        session.add(purchase)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            claimed = session.exec(
                _select(Purchase).where(Purchase.tx_hash == tx_hash_norm)
            ).first()  # type: ignore[misc]
            if claimed is None:
                raise HTTPException(
                    status_code=409,
                    detail="purchase already in progress for this transaction",
                )
            if _normalize_hex(claimed.buyer_address or "") != buyer_norm:
                raise HTTPException(
                    status_code=409,
                    detail="transaction already claimed by a different wallet",
                )
            purchase = claimed
            pur_id = purchase.purchase_id
            if purchase.status == "fulfilled" and purchase.subkey:
                return {
                    "purchaseId": purchase.purchase_id,
                    "status": purchase.status,
                    "tenantId": purchase.tenant_id,
                    "subkey": purchase.subkey,
                    "expiresAt": (
                        purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                        if purchase.expires_at
                        else None
                    ),
                }

        try:
            expires_at = datetime.utcfromtimestamp(int(time.time()) + 24 * 3600)
            parent_key = (
                _get_env("VENICE_PARENT_KEY") or _get_env("VENICE_API_KEY") or ""
            ).strip()
            if not parent_key:
                raise HTTPException(
                    status_code=503, detail="venice parent key not configured"
                )
            sub = _keys.issue_scoped_key(
                parent_key,
                label=f"Buyer {buyer_norm[2:8]}...",
                consumption_limit=consumption,
                expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            subkey = _extract_field(
                sub, ("apiKey", "api_key", "key", "token", "api_key_value")
            )
            kid_val = _extract_field(sub, ("id", "keyId", "apiKeyId", "api_key_id"))
            kid = kid_val or None
            if not subkey:
                raise RuntimeError("failed to mint subkey")

            _store.upsert(
                Tenant(
                    id=tenant_id,
                    label="Buyer",
                    subkey=subkey,
                    quota=float(quote.units),
                    expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    owner_address=buyer_norm,
                    key_id=kid,
                )
            )
            purchase.status = "fulfilled"
            purchase.tenant_id = tenant_id
            purchase.subkey = subkey
            purchase.key_id = kid
            purchase.expires_at = expires_at
            purchase.fulfilled_at = datetime.utcnow()
            quote.status = "filled"
        except HTTPException:
            raise
        except Exception as exc:
            _logger.warning(
                "purchase verify: key issuance failed for purchase %s: %s", pur_id, exc
            )
            purchase.status = "confirmed"

        session.add(purchase)
        session.add(quote)
        session.commit()

        result = {
            "purchaseId": purchase.purchase_id,
            "status": purchase.status,
            "tenantId": purchase.tenant_id,
            "subkey": purchase.subkey,
            "expiresAt": (
                purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if purchase.expires_at
                else None
            ),
        }

        try:
            from libs.telemetry.events import emit as emit_event

            emit_event(
                "purchase.verified",
                {
                    **result,
                    "txHash": tx_hash_norm,
                    "asset": quote.asset,
                    "units": float(quote.units),
                },
            )
        except Exception:
            pass

        return result


@router.get(
    "/v1/purchases/{purchase_id}",
    response_model=PurchaseStatus,
    dependencies=[Depends(enforce_public_rate_limit)],
)
def get_purchase(purchase_id: str) -> dict:
    """Status polling only: never exposes the key.

    Purchase ids are derivable from public chain data, so the key itself is
    returned exclusively through wallet-signed flows (verify/recover).
    """
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")
    with next(get_session()) as session:  # type: ignore[call-arg]
        try:
            purchase = session.exec(
                _select(Purchase).where(Purchase.purchase_id == purchase_id)
            ).first()  # type: ignore[misc]
        except Exception as exc:
            # Handle missing tables in fresh deployments (UndefinedTable / relation does not exist).
            # Also handle InFailedSqlTransaction which occurs when a transaction is aborted.
            text = f"{type(exc).__name__}: {exc}"
            is_table_error = (
                "UndefinedTable" in text
                or 'relation "purchase" does not exist' in text.lower()
                or "InFailedSqlTransaction" in text
                or "current transaction is aborted" in text.lower()
            )
            if is_table_error and callable(create_all_unconditional):
                # Rollback the failed transaction before retrying
                try:
                    session.rollback()
                except Exception:
                    pass
                try:
                    create_all_unconditional()  # type: ignore[misc]
                except Exception:
                    pass
                # Re-run the lookup after best-effort bootstrap and rollback
                try:
                    purchase = session.exec(
                        _select(Purchase).where(Purchase.purchase_id == purchase_id)
                    ).first()  # type: ignore[misc]
                except Exception as retry_exc:
                    # If retry still fails, rollback and raise
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    raise retry_exc
            else:
                raise
        if purchase is None:
            raise HTTPException(status_code=404, detail="purchase not found")
        return {
            "purchaseId": purchase.purchase_id,
            "status": purchase.status,
            "tenantId": purchase.tenant_id,
            "subkey": None,
            "keyIssued": bool(purchase.subkey),
            "expiresAt": (
                purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if purchase.expires_at
                else None
            ),
        }


@router.get(
    "/v1/purchases/{purchase_id}/stream",
    dependencies=[Depends(enforce_public_rate_limit)],
)
def purchases_stream(purchase_id: str) -> StreamingResponse:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")

    def _gen():
        last_status = None
        last_heartbeat = time.time()
        not_found_count = 0
        MAX_NOT_FOUND_RETRIES = 3
        started_at = time.time()
        max_lifetime = _stream_max_seconds()
        terminal_statuses = {"fulfilled", "failed", "expired", "cancelled"}
        while True:
            try:
                now = time.time()
                if max_lifetime and (now - started_at) >= max_lifetime:
                    yield "event: end\ndata: stream timeout\n\n"
                    break
                if now - last_heartbeat >= 30.0:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now

                with next(get_session()) as session:  # type: ignore[call-arg]
                    try:
                        purchase = session.exec(
                            _select(Purchase).where(Purchase.purchase_id == purchase_id)
                        ).first()  # type: ignore[misc]
                    except Exception as exc:
                        # Handle missing tables in fresh deployments (UndefinedTable / relation does not exist).
                        # Also handle InFailedSqlTransaction which occurs when a transaction is aborted.
                        text = f"{type(exc).__name__}: {exc}"
                        is_table_error = (
                            "UndefinedTable" in text
                            or 'relation "purchase" does not exist' in text.lower()
                            or "InFailedSqlTransaction" in text
                            or "current transaction is aborted" in text.lower()
                        )
                        if is_table_error and callable(create_all_unconditional):
                            # Rollback the failed transaction before retrying
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            try:
                                create_all_unconditional()  # type: ignore[misc]
                            except Exception:
                                pass
                            # Re-run the lookup after best-effort bootstrap and rollback
                            try:
                                purchase = session.exec(
                                    _select(Purchase).where(
                                        Purchase.purchase_id == purchase_id
                                    )
                                ).first()  # type: ignore[misc]
                            except Exception as retry_exc:
                                # If retry still fails, rollback and raise
                                try:
                                    session.rollback()
                                except Exception:
                                    pass
                                raise retry_exc
                        else:
                            # Re-raise non-table errors; they'll be caught by outer try-except
                            raise
                    if purchase is None:
                        not_found_count += 1
                        yield "event: error\n" + "data: not found\n\n"
                        if not_found_count >= MAX_NOT_FOUND_RETRIES:
                            yield (
                                "event: error\n"
                                "data: purchase not found after multiple attempts, terminating stream\n\n"
                            )
                            break
                        time.sleep(3)
                        continue
                    # Reset counter when purchase is found
                    not_found_count = 0
                    if purchase.status != last_status:
                        payload = {
                            "purchaseId": purchase.purchase_id,
                            "status": purchase.status,
                            "tenantId": purchase.tenant_id,
                            # Keys are only released via wallet-signed flows.
                            "keyIssued": bool(purchase.subkey),
                            "expiresAt": (
                                purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                                if purchase.expires_at
                                else None
                            ),
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                        last_status = purchase.status
                        if purchase.status in terminal_statuses or purchase.subkey:
                            yield "event: end\ndata: terminal state\n\n"
                            break
                    elif purchase.status in terminal_statuses or purchase.subkey:
                        yield "event: end\ndata: terminal state\n\n"
                        break
            except Exception as exc:
                yield f"event: error\ndata: {exc!s}\n\n"
            time.sleep(3)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


@router.post(
    "/v1/settlement/confirm",
    response_model=PurchaseStatus,
    dependencies=[Depends(enforce_public_rate_limit)],
)
def settlement_confirm(req: PurchaseVerifyRequest) -> dict:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")
    return verify_purchase(req)


def _get_env(name: str) -> str | None:
    return os.getenv(name)


__all__ = [
    "configured_payment_assets",
    "init_router",
    "payment_asset_supported",
    "router",
]
