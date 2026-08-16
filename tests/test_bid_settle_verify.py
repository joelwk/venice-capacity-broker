"""Bid → settle → verify → key is one path.

Settle persists a quote and links Bid.quote_id. Verify mints the key and
marks the bid filled. Expired or out-of-band settle is 409. Buyer mismatch
on a bid-linked quote is 400.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from types import ModuleType
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import NoSuchModuleError

for _mod in list(sys.modules):
    if _mod in ("db.session", "db.models") or _mod.split(".", 1)[0] in (
        "sqlmodel",
        "sqlalchemy",
    ):
        sys.modules.pop(_mod, None)

pytest.importorskip("sqlmodel")
pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

TREASURY = "0xabc0000000000000000000000000000000000001"
ATTACKER = "0xbad0000000000000000000000000000000000003"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

UNIT_PRICE_USDC = 100_000
UNITS_MICRO = 5_000_000
MAX_PRICE_USDC = 2_000_000
LOW_MAX_PRICE_USDC = 1_000
RECEIPT_BLOCK = 12_345
CURRENT_BLOCK = 12_545
BASE_CHAIN_ID = 8453
BASE_CHAIN_ID_HEX = "0x2105"
DOMAIN = "Venice Broker"
DOMAIN_VERSION = "1"


def _ensure_sqlite_dialect() -> None:
    try:
        from sqlalchemy.dialects import registry

        registry.load("sqlite")
    except Exception as exc:  # pragma: no cover - env specific
        if (
            isinstance(exc, (NoSuchModuleError, AttributeError))
            or "sqlite" in str(exc).lower()
        ):
            pytest.skip("sqlite dialect unavailable in current environment")
        raise


def _load_broker_app_module() -> ModuleType:
    _ensure_sqlite_dialect()
    os.environ.setdefault("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    os.environ.setdefault("VENICE_PARENT_KEY", "parent-test")
    spec = importlib.util.spec_from_file_location(
        "broker_app_bid_settle_test", "apps/broker_api/app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _pad_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def _make_rpc(*, receipt: dict, chain_id_hex: str = BASE_CHAIN_ID_HEX):
    def _fake_rpc(urls, method, params):
        if method == "eth_chainId":
            return chain_id_hex
        if method == "eth_blockNumber":
            return hex(CURRENT_BLOCK)
        if method == "eth_getTransactionReceipt":
            return receipt
        if method == "eth_getTransactionByHash":
            return {"to": TREASURY, "from": TREASURY, "value": "0x0"}
        raise AssertionError(f"unexpected rpc call: {method}")

    return _fake_rpc


def _typed_bid(buyer: str, **overrides) -> dict:
    payload = {
        "buyer": buyer,
        "units": UNITS_MICRO,
        "maxPrice": MAX_PRICE_USDC,
        "asset": "USDC",
        "expiry": int(time.time()) + 3600,
        "slippageBps": 50,
        "nonce": int(time.time() * 1000),
        "chainId": BASE_CHAIN_ID,
    }
    payload.update(overrides)
    return payload


def _sign_bid(account, payload: dict) -> str:
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "PurchaseIntent": [
                {"name": "buyer", "type": "address"},
                {"name": "units", "type": "uint256"},
                {"name": "maxPrice", "type": "uint256"},
                {"name": "asset", "type": "string"},
                {"name": "expiry", "type": "uint256"},
                {"name": "slippageBps", "type": "uint16"},
                {"name": "nonce", "type": "uint256"},
                {"name": "chainId", "type": "uint256"},
            ],
        },
        "primaryType": "PurchaseIntent",
        "domain": {
            "name": DOMAIN,
            "version": DOMAIN_VERSION,
            "chainId": int(payload["chainId"]),
        },
        "message": {
            "buyer": payload["buyer"],
            "units": int(payload["units"]),
            "maxPrice": int(payload["maxPrice"]),
            "asset": str(payload["asset"]),
            "expiry": int(payload["expiry"]),
            "slippageBps": int(payload["slippageBps"]),
            "nonce": int(payload["nonce"]),
            "chainId": int(payload["chainId"]),
        },
    }
    msg = encode_typed_data(full_message=typed)
    signed = account.sign_message(msg)
    return "0x" + signed.signature.hex()


def _cheap_clearing(*_args, **_kwargs) -> dict:
    return {
        "price": 0.05,
        "bandMin": 0.049,
        "bandMax": 0.051,
        "bandBps": 200,
        "ts": int(time.time()),
    }


def _bootstrap(monkeypatch, tmp_path, *, db_name: str) -> tuple[TestClient, ModuleType]:
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("BIDS_ENABLED", "true")
    monkeypatch.setenv("CLEARING_ENABLED", "false")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_USDC", str(UNIT_PRICE_USDC))
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", TREASURY)
    monkeypatch.setenv("CHAIN_ID", str(BASE_CHAIN_ID))
    monkeypatch.setenv("SIGN_DOMAIN_NAME", DOMAIN)
    monkeypatch.setenv("SIGN_DOMAIN_VERSION", DOMAIN_VERSION)
    monkeypatch.setenv("BUY_RATE_LIMITS_ENABLED", "false")
    monkeypatch.setenv("QUOTES_PERSIST_ENABLED", "true")
    monkeypatch.setenv("BROKER_WARMUP_MARKETDATA_TIMEOUT_SECONDS", "1")

    db_path = tmp_path / db_name
    store_path = tmp_path / f"{db_name}.store.json"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

    mock_mdp = Mock()
    mock_mdp.prices.return_value = {
        "DIEM": 0.05,
        "VVV": 0.05,
        "ETH": 3000.0,
        "USDC": 1.0,
    }
    monkeypatch.setattr(
        "apps.broker_api.marketdata.get_marketdata_provider",
        lambda *args, **kwargs: mock_mdp,
    )
    monkeypatch.setattr(
        "apps.broker_api.services.clearing.compute_clearing_price",
        _cheap_clearing,
    )

    mod = _load_broker_app_module()
    monkeypatch.setattr(mod.purchases, "_challenge_store", {}, raising=False)
    monkeypatch.setattr(
        mod.quotes,
        "_diem_price_snap",
        lambda: {"symbol": "DIEM", "priceUsd": 1.0},
        raising=False,
    )
    monkeypatch.setattr(
        mod.keys,
        "issue_scoped_key",
        lambda parent_key, label, consumption_limit, expires_at=None: {
            "apiKey": "sk-bid-test",  # gitleaks:allow test API key
            "id": "kid-bid",
        },
        raising=True,
    )
    return TestClient(mod.app), mod


def _create_bid(client: TestClient, account, **overrides) -> tuple[dict, dict]:
    payload = _typed_bid(account.address, **overrides)
    payload["signature"] = _sign_bid(account, payload)
    resp = client.post("/v1/bids", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json(), payload


def _signature_fields(
    client: TestClient,
    mod: ModuleType,
    monkeypatch,
    *,
    tx_hash: str,
    buyer: str,
    recovered: str | None = None,
) -> dict:
    signer = recovered or buyer

    class FakeAccount:
        @staticmethod
        def recover_message(message, signature):
            return signer

    monkeypatch.setattr(mod.purchases, "Account", FakeAccount, raising=False)
    monkeypatch.setattr(
        mod.purchases, "encode_defunct", lambda text: text, raising=False
    )
    challenge = client.get(
        "/v1/purchases/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer},
    )
    assert challenge.status_code == 200, challenge.text
    return {"signature": "sig-test", "nonce": challenge.json()["nonce"]}


def test_bid_settle_verify_mints_key_and_fills_bid(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="bid-happy.db")
    account = Account.create()
    created, _payload = _create_bid(client, account)

    settle = client.post(f"/v1/settlement/{created['bidId']}/settle")
    assert settle.status_code == 200, settle.text
    quote = settle.json()
    assert quote["quoteId"]
    assert quote["asset"] == "USDC"

    listed = client.get(f"/v1/bids/{created['bidId']}")
    assert listed.status_code == 200, listed.text
    assert listed.json()["quoteId"] == quote["quoteId"]
    assert listed.json()["status"] == "accepted_window"

    tx_hash = "0x" + "b" * 64
    receipt = {
        "status": "0x1",
        "blockNumber": hex(RECEIPT_BLOCK),
        "logs": [
            {
                "address": USDC_BASE,
                "topics": [
                    TRANSFER_SIG,
                    _pad_topic(account.address),
                    _pad_topic(TREASURY),
                ],
                "data": hex(int(quote["totalPrice"])),
            }
        ],
    }
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(receipt=receipt), raising=True
    )
    fields = _signature_fields(
        client, mod, monkeypatch, tx_hash=tx_hash, buyer=account.address
    )

    verify = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": account.address,
            **fields,
        },
    )
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["status"] == "fulfilled"
    assert body["subkey"] == "sk-bid-test"  # gitleaks:allow test API key

    filled = client.get(f"/v1/bids/{created['bidId']}")
    assert filled.status_code == 200, filled.text
    assert filled.json()["status"] == "filled"
    assert filled.json()["quoteId"] == quote["quoteId"]


def test_settle_expired_bid_is_conflict(monkeypatch, tmp_path):
    client, _mod = _bootstrap(monkeypatch, tmp_path, db_name="bid-expired.db")
    account = Account.create()
    created, _payload = _create_bid(
        client, account, expiry=int(time.time()) - 30, nonce=11
    )

    settle = client.post(f"/v1/settlement/{created['bidId']}/settle")
    assert settle.status_code == 409, settle.text
    assert "expired" in settle.json().get("detail", "").lower()


def test_settle_out_of_band_bid_is_conflict(monkeypatch, tmp_path):
    client, _mod = _bootstrap(monkeypatch, tmp_path, db_name="bid-oob.db")
    account = Account.create()
    created, _payload = _create_bid(
        client, account, maxPrice=LOW_MAX_PRICE_USDC, nonce=22
    )

    settle = client.post(f"/v1/settlement/{created['bidId']}/settle")
    assert settle.status_code == 409, settle.text
    assert "out of band" in settle.json().get("detail", "").lower()


def test_verify_rejects_buyer_mismatch_on_bid_quote(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="bid-mismatch.db")
    account = Account.create()
    created, _payload = _create_bid(client, account, nonce=33)

    settle = client.post(f"/v1/settlement/{created['bidId']}/settle")
    assert settle.status_code == 200, settle.text
    quote = settle.json()

    tx_hash = "0x" + "c" * 64
    receipt = {
        "status": "0x1",
        "blockNumber": hex(RECEIPT_BLOCK),
        "logs": [
            {
                "address": USDC_BASE,
                "topics": [
                    TRANSFER_SIG,
                    _pad_topic(ATTACKER),
                    _pad_topic(TREASURY),
                ],
                "data": hex(int(quote["totalPrice"])),
            }
        ],
    }
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(receipt=receipt), raising=True
    )
    fields = _signature_fields(
        client, mod, monkeypatch, tx_hash=tx_hash, buyer=ATTACKER, recovered=ATTACKER
    )

    verify = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": ATTACKER,
            **fields,
        },
    )
    assert verify.status_code == 400, verify.text
    assert "buyer" in verify.json().get("detail", "").lower()
