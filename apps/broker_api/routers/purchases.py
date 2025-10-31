from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from db.session import create_db_and_tables, get_session
from db.models import Purchase, Quote
from sqlmodel import select as _select  # type: ignore
from ..models import PurchaseStatus, PurchaseVerifyRequest
from ..tenant_store import Tenant, TenantStore
from services.venice_keys.manager import KeyManager

router = APIRouter()

_store: TenantStore
_keys: KeyManager
_logger: logging.Logger = logging.getLogger("broker.api.purchases")
_extract_field: Callable[[dict, tuple[str, ...]], str]
_purchases_enabled: bool


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
    _purchases_enabled = purchases_enabled

    if not purchases_enabled:
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


def _rpc_call(url: str, method: str, params: list) -> dict:
    import requests

    response = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload["result"]


def _verify_tx(
    quote: Quote,
    tx_hash: str,
    treasury: str,
    usdc_addr: Optional[str],
    base_rpc: str,
    buyer_address: str,
) -> tuple[int, dict]:
    tx_hash = _normalize_hex(tx_hash)
    buyer_address = _normalize_hex(buyer_address)
    if not tx_hash or len(tx_hash) != 66:
        raise RuntimeError("transaction hash required")
    if not buyer_address or len(buyer_address) != 42:
        raise RuntimeError("buyer address invalid")
    treasury_norm = _normalize_hex(treasury)
    usdc_norm = _normalize_hex(usdc_addr) if usdc_addr else None
    receipt = _rpc_call(base_rpc, "eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        raise RuntimeError("transaction not found")
    status_hex = receipt.get("status") or "0x0"
    if _wei(status_hex) != 1:
        raise RuntimeError("transaction failed")
    logs = receipt.get("logs") or []
    total_paid = 0
    verification: dict[str, dict] = {}
    for idx, log in enumerate(logs):
        topics = [str(t).lower() for t in log.get("topics") or []]
        if len(topics) < 3:
            continue
        transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        if topics[0] != transfer_sig:
            continue
        from_addr = _normalize_hex(topics[1])
        to_addr = _normalize_hex(topics[2])
        if to_addr != treasury_norm:
            continue
        if usdc_norm and _normalize_hex(log.get("address")) != usdc_norm:
            continue
        data_hex = str(log.get("data") or "0x0")
        amount = _wei(data_hex)
        total_paid += amount
        verification[str(idx)] = {
            "from": from_addr,
            "to": to_addr,
            "amount": amount,
            "token": _normalize_hex(log.get("address")),
        }
    if total_paid <= 0:
        raise RuntimeError("no treasury transfer found")
    return total_paid, verification


@router.post("/v1/purchases/verify", response_model=PurchaseStatus)
def verify_purchase(req: PurchaseVerifyRequest) -> dict:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")

    base_rpc = (_get_env("BASE_RPC_URL") or "").strip()
    treasury = (_get_env("TREASURY_ADDRESS") or "").strip()
    if not treasury:
        raise HTTPException(status_code=400, detail="TREASURY_ADDRESS not set")
    usdc_addr = (_get_env("USDC_ADDRESS") or "").strip() or None
    if not base_rpc:
        raise HTTPException(status_code=400, detail="BASE_RPC_URL not set")

    buyer_norm = _normalize_hex(req.buyerAddress)
    if not buyer_norm or len(buyer_norm) != 42:
        raise HTTPException(status_code=400, detail="buyerAddress invalid")

    tx_hash_norm = _normalize_hex(req.txHash)
    if not tx_hash_norm or len(tx_hash_norm) != 66:
        raise HTTPException(status_code=400, detail="txHash invalid")

    allowlist_raw = (_get_env("PURCHASE_TENANT_ALLOWLIST") or "")
    tenant_allow = {item.strip().lower() for item in allowlist_raw.split(",") if item.strip()}
    default_tenant = "w:" + buyer_norm.lower()

    with next(get_session()) as session:  # type: ignore[call-arg]
        pur_id = hashlib.sha256(f"{req.txHash}:{req.buyerAddress}".encode()).hexdigest()[:16]
        existing = session.exec(_select(Purchase).where(Purchase.purchase_id == pur_id)).first()  # type: ignore[misc]
        if existing is None and (req.txHash != tx_hash_norm or req.buyerAddress != buyer_norm):
            alt_id = hashlib.sha256(f"{tx_hash_norm}:{buyer_norm}".encode()).hexdigest()[:16]
            existing = session.exec(_select(Purchase).where(Purchase.purchase_id == alt_id)).first()  # type: ignore[misc]
            if existing is not None:
                pur_id = existing.purchase_id

        quote_id = req.quoteId
        if existing and existing.quote_id:
            if req.quoteId and existing.quote_id != req.quoteId:
                raise HTTPException(status_code=409, detail="purchase already recorded for a different quote")
            quote_id = existing.quote_id

        quote = session.exec(_select(Quote).where(Quote.quote_id == quote_id)).first()  # type: ignore[misc]
        if quote is None:
            raise HTTPException(status_code=404, detail="quote not found")

        if existing and existing.status == "fulfilled" and existing.subkey:
            expires_iso = existing.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if existing.expires_at else None
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
            raise HTTPException(status_code=409, detail=f"quote not open (status={quote.status})")

        try:
            paid_val, receipt = _verify_tx(quote, tx_hash_norm, treasury, usdc_addr, base_rpc, buyer_norm)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        amount_paid = int(paid_val or quote.total_price)
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
            if requested_id.lower() == default_tenant:
                tenant_id = requested_id
            elif requested_id.lower() in tenant_allow:
                tenant_id = requested_id
            else:
                raise HTTPException(status_code=403, detail="tenantId override not permitted")

        existing_tenant = _store.get(tenant_id)
        if existing_tenant and existing_tenant.owner_address and existing_tenant.owner_address.lower() != buyer_norm:
            raise HTTPException(status_code=409, detail="tenant already assigned to a different wallet")
        if existing_tenant and existing_tenant.owner_address is None and tenant_id != default_tenant:
            raise HTTPException(status_code=409, detail="tenant reserved for administrator")

        limit_kind = (_get_env("PURCHASE_UNITS_KIND") or "diem").strip().lower()
        try:
            units_value = float(quote.units)
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="quote units invalid") from exc
        if units_value <= 0:
            raise HTTPException(status_code=400, detail="quote units must be positive")
        if limit_kind == "diem":
            consumption = {"diem": round(units_value, 12)}
        else:
            consumption = {limit_kind: max(1, int(math.ceil(units_value)))}

        try:
            expires_at = datetime.utcfromtimestamp(int(time.time()) + 24 * 3600)
            parent_key = (_get_env("VENICE_PARENT_KEY") or _get_env("VENICE_API_KEY") or "").strip()
            if not parent_key:
                raise HTTPException(status_code=503, detail="venice parent key not configured")
            sub = _keys.issue_scoped_key(
                parent_key,
                label=f"Buyer {buyer_norm[2:8]}...",
                consumption_limit=consumption,
                expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            subkey = _extract_field(sub, ("apiKey", "api_key", "key", "token", "api_key_value"))
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
        except Exception as exc:  # noqa: BLE001
            _logger.warning("purchase verify: key issuance failed for purchase %s: %s", pur_id, exc)
            purchase.status = "confirmed"

        session.add(purchase)
        session.add(quote)
        session.commit()

        result = {
            "purchaseId": purchase.purchase_id,
            "status": purchase.status,
            "tenantId": purchase.tenant_id,
            "subkey": purchase.subkey,
            "expiresAt": purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if purchase.expires_at else None,
        }

        try:
            from libs.telemetry.events import emit as emit_event

            emit_event("purchase.verified", {**result, "txHash": tx_hash_norm, "asset": quote.asset, "units": float(quote.units)})
        except Exception:
            pass

        return result


@router.get("/v1/purchases/{purchase_id}", response_model=PurchaseStatus)
def get_purchase(purchase_id: str) -> dict:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")
    with next(get_session()) as session:  # type: ignore[call-arg]
        purchase = session.exec(_select(Purchase).where(Purchase.purchase_id == purchase_id)).first()  # type: ignore[misc]
        if purchase is None:
            raise HTTPException(status_code=404, detail="purchase not found")
        return {
            "purchaseId": purchase.purchase_id,
            "status": purchase.status,
            "tenantId": purchase.tenant_id,
            "subkey": purchase.subkey,
            "expiresAt": purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if purchase.expires_at else None,
        }


@router.get("/v1/purchases/{purchase_id}/stream")
def purchases_stream(purchase_id: str) -> StreamingResponse:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")

    def _gen():
        last_status = None
        last_heartbeat = time.time()
        while True:
            try:
                now = time.time()
                if now - last_heartbeat >= 30.0:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now

                with next(get_session()) as session:  # type: ignore[call-arg]
                    purchase = session.exec(
                        _select(Purchase).where(Purchase.purchase_id == purchase_id)
                    ).first()  # type: ignore[misc]
                    if purchase is None:
                        yield "event: error\n" + "data: not found\n\n"
                        time.sleep(3)
                        continue
                    if purchase.status != last_status:
                        payload = {
                            "purchaseId": purchase.purchase_id,
                            "status": purchase.status,
                            "tenantId": purchase.tenant_id,
                            "subkey": purchase.subkey,
                            "expiresAt": purchase.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if purchase.expires_at else None,
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                        last_status = purchase.status
            except Exception as exc:  # noqa: BLE001
                yield f"event: error\n" f"data: {str(exc)}\n\n"
            time.sleep(3)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


@router.post("/v1/settlement/confirm", response_model=PurchaseStatus)
def settlement_confirm(req: PurchaseVerifyRequest) -> dict:
    if not _purchases_enabled:
        raise HTTPException(status_code=404, detail="purchases disabled")
    return verify_purchase(req)


def _get_env(name: str) -> Optional[str]:
    return os.getenv(name)


__all__ = ["router", "init_router"]
