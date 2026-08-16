"""Security regression tests for the public buy flow.

Covers the payment-path hardening required before public launch:
- wallet-signature gating on /v1/purchases/verify
- payment amount enforcement against the quote
- ERC-20 Transfer topic de-padding (USDC payments)
- no API-key disclosure on unauthenticated status endpoints
- fail-closed asset -> token address mapping
- confirmation depth and chain-id checks
- unique tx_hash constraint (double-mint race)
- per-IP rate limiting on public endpoints
"""

from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import NoSuchModuleError

# Evict stub/real module trees so the app loads against real sqlmodel.
for _mod in list(sys.modules):
    if _mod in ("db.session", "db.models") or _mod.split(".", 1)[0] in (
        "sqlmodel",
        "sqlalchemy",
    ):
        sys.modules.pop(_mod, None)

pytest.importorskip("sqlmodel")

TREASURY = "0xabc0000000000000000000000000000000000001"
BUYER = "0xdef0000000000000000000000000000000000002"
ATTACKER = "0xbad0000000000000000000000000000000000003"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

UNIT_PRICE_ETH_WEI = 10**15
UNIT_PRICE_USDC = 100_000  # $0.10 in 6-decimals

RECEIPT_BLOCK = 12_345
CURRENT_BLOCK = 12_545  # comfortably beyond any confirmation requirement
BASE_CHAIN_ID_HEX = "0x2105"  # 8453


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
        "broker_app_security_test", "apps/broker_api/app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _pad_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def _make_rpc(
    *,
    tx_payload: dict | None = None,
    receipt: dict | None = None,
    chain_id_hex: str = BASE_CHAIN_ID_HEX,
    current_block: int = CURRENT_BLOCK,
):
    def _fake_rpc(urls, method, params):
        if method == "eth_chainId":
            return chain_id_hex
        if method == "eth_blockNumber":
            return hex(current_block)
        if method == "eth_getTransactionReceipt":
            if receipt is not None:
                return receipt
            return {"status": "0x1", "blockNumber": hex(RECEIPT_BLOCK), "logs": []}
        if method == "eth_getTransactionByHash":
            return tx_payload
        raise AssertionError(f"unexpected rpc call: {method}")

    return _fake_rpc


def _bootstrap(
    monkeypatch,
    tmp_path,
    *,
    db_name: str,
    rate_limits: bool = False,
) -> tuple[TestClient, ModuleType]:
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(UNIT_PRICE_ETH_WEI))
    monkeypatch.setenv("PRICE_UNIT_USDC", str(UNIT_PRICE_USDC))
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", TREASURY)
    if not rate_limits:
        monkeypatch.setenv("BUY_RATE_LIMITS_ENABLED", "false")

    db_path = tmp_path / db_name
    store_path = tmp_path / f"{db_name}.store.json"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

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
            "apiKey": "sk-security-test",  # gitleaks:allow test API key
            "id": "kid-security",
        },
        raising=True,
    )
    return TestClient(mod.app), mod


def _quote(client: TestClient, units: float, asset: str) -> dict:
    resp = client.get("/v1/quotes", params={"units": units, "asset": asset})
    assert resp.status_code == 200, resp.text
    return resp.json()


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


def test_challenge_endpoint_issues_nonce(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="challenge.db")

    class FakeAccount:
        @staticmethod
        def recover_message(message, signature):
            return BUYER

    monkeypatch.setattr(mod.purchases, "Account", FakeAccount, raising=False)
    monkeypatch.setattr(
        mod.purchases, "encode_defunct", lambda text: text, raising=False
    )

    resp = client.get(
        "/v1/purchases/challenge",
        params={"txHash": "0x" + "a" * 64, "buyerAddress": BUYER},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nonce"]
    assert body["message"]


def test_verify_requires_wallet_signature(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="sig-required.db")
    tx_hash = "0x" + "1" * 64
    quote = _quote(client, 5, "ETH")
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(int(quote["totalPrice"]))}
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(tx_payload=tx_payload), raising=True
    )

    resp = client.post(
        "/v1/purchases/verify",
        json={"quoteId": quote["quoteId"], "txHash": tx_hash, "buyerAddress": BUYER},
    )
    assert resp.status_code == 401, resp.text
    assert "signature" in resp.json().get("detail", "").lower()


def test_verify_rejects_signature_from_other_wallet(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="sig-mismatch.db")
    tx_hash = "0x" + "2" * 64
    quote = _quote(client, 5, "ETH")
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(int(quote["totalPrice"]))}
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(tx_payload=tx_payload), raising=True
    )
    fields = _signature_fields(
        client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER, recovered=ATTACKER
    )

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert resp.status_code == 403, resp.text


def test_signed_verify_happy_path_eth(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="happy-eth.db")
    tx_hash = "0x" + "3" * 64
    quote = _quote(client, 5, "ETH")
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(int(quote["totalPrice"]))}
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(tx_payload=tx_payload), raising=True
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "fulfilled"
    assert body["subkey"] == "sk-security-test"  # gitleaks:allow test API key


def test_verify_rejects_underpayment(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="underpay.db")
    tx_hash = "0x" + "4" * 64
    quote = _quote(client, 5, "ETH")
    underpaid = int(quote["totalPrice"]) // 2
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(underpaid)}
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(tx_payload=tx_payload), raising=True
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "insufficient" in resp.json().get("detail", "").lower()


def test_verify_accepts_padded_erc20_usdc_transfer(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="usdc-topics.db")
    tx_hash = "0x" + "5" * 64
    quote = _quote(client, 5, "USDC")
    receipt = {
        "status": "0x1",
        "blockNumber": hex(RECEIPT_BLOCK),
        "logs": [
            {
                "address": USDC_BASE,
                "topics": [TRANSFER_SIG, _pad_topic(BUYER), _pad_topic(TREASURY)],
                "data": hex(int(quote["totalPrice"])),
            }
        ],
    }
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(receipt=receipt), raising=True
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "fulfilled"


def test_purchase_status_does_not_leak_subkey(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="status-leak.db")
    tx_hash = "0x" + "6" * 64
    quote = _quote(client, 5, "ETH")
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(int(quote["totalPrice"]))}
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(tx_payload=tx_payload), raising=True
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    verify = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert verify.status_code == 200, verify.text
    purchase_id = verify.json()["purchaseId"]

    status = client.get(f"/v1/purchases/{purchase_id}")
    assert status.status_code == 200, status.text
    assert status.json().get("subkey") is None


def test_env_does_not_leak_rpc_url(monkeypatch, tmp_path):
    secret_rpc = "https://secret-rpc.example/v2/super-secret-key"
    client, _mod = _bootstrap(monkeypatch, tmp_path, db_name="env-rpc-leak.db")
    monkeypatch.setenv("BASE_RPC_URL", secret_rpc)
    monkeypatch.setenv("BASE_RPC_URLS", secret_rpc)

    env = client.get("/v1/env")
    assert env.status_code == 200, env.text
    body = env.json()
    dumped = env.text.lower()
    assert secret_rpc.lower() not in dumped
    assert "super-secret-key" not in dumped
    assert "base_rpc_url" not in body.get("network", {})
    assert body["network"]["rpc_configured"] is True


def _clear_wbtc_token_env(monkeypatch) -> None:
    for name in ("WBTC_TOKEN_ADDRESS", "WBTC_ADDRESS"):
        monkeypatch.delenv(name, raising=False)


def test_quote_rejects_wbtc_without_token_address(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="wbtc-quote-closed.db")
    _clear_wbtc_token_env(monkeypatch)

    resp = client.get("/v1/quotes", params={"units": 1, "asset": "WBTC"})
    assert resp.status_code == 400, resp.text
    assert "payment token address not configured" in resp.json()["detail"]
    env = client.get("/v1/env")
    assert env.status_code == 200, env.text
    assert "WBTC" not in env.json()["payments"]["accepted_assets"]


def test_wbtc_token_address_unlocks_quotes_and_env_assets(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="wbtc-token-alias.db")
    _clear_wbtc_token_env(monkeypatch)
    wbtc = "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c"
    monkeypatch.setenv("WBTC_TOKEN_ADDRESS", wbtc)

    assert mod.purchases.payment_asset_supported("WBTC")
    assert mod.purchases._asset_to_token_address("WBTC") == wbtc
    env = client.get("/v1/env")
    assert env.status_code == 200, env.text
    assert "WBTC" in env.json()["payments"]["accepted_assets"]


def test_verify_fails_closed_for_unmapped_asset(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="wbtc-closed.db")
    _clear_wbtc_token_env(monkeypatch)
    tx_hash = "0x" + "7" * 64

    from datetime import UTC, datetime, timedelta

    from db.models import Quote
    from db.session import get_session

    quote_id = "q-wbtc-test"
    with next(get_session()) as session:
        session.add(
            Quote(
                quote_id=quote_id,
                units=5.0,
                asset="WBTC",
                unit_price=10**5,
                total_price=5 * 10**5,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                status="open",
            )
        )
        session.commit()

    rogue_token = "0x9999999999999999999999999999999999999999"
    receipt = {
        "status": "0x1",
        "blockNumber": hex(RECEIPT_BLOCK),
        "logs": [
            {
                "address": rogue_token,
                "topics": [TRANSFER_SIG, _pad_topic(BUYER), _pad_topic(TREASURY)],
                "data": hex(5 * 10**5),
            }
        ],
    }
    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _make_rpc(receipt=receipt), raising=True
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    resp = client.post(
        "/v1/purchases/verify",
        json={"quoteId": quote_id, "txHash": tx_hash, "buyerAddress": BUYER, **fields},
    )
    assert resp.status_code == 400, resp.text
    assert "not supported" in resp.json().get("detail", "").lower()


def test_verify_rejects_insufficient_confirmations(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="confirmations.db")
    tx_hash = "0x" + "8" * 64
    quote = _quote(client, 5, "ETH")
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(int(quote["totalPrice"]))}
    monkeypatch.setattr(
        mod.purchases,
        "_rpc_call",
        _make_rpc(tx_payload=tx_payload, current_block=RECEIPT_BLOCK),
        raising=True,
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "confirmation" in resp.json().get("detail", "").lower()


def test_verify_rejects_wrong_chain(monkeypatch, tmp_path):
    client, mod = _bootstrap(monkeypatch, tmp_path, db_name="wrong-chain.db")
    tx_hash = "0x" + "9" * 64
    quote = _quote(client, 5, "ETH")
    tx_payload = {"to": TREASURY, "from": BUYER, "value": hex(int(quote["totalPrice"]))}
    monkeypatch.setattr(
        mod.purchases,
        "_rpc_call",
        _make_rpc(tx_payload=tx_payload, chain_id_hex="0x1"),
        raising=True,
    )
    fields = _signature_fields(client, mod, monkeypatch, tx_hash=tx_hash, buyer=BUYER)

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": BUYER,
            **fields,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "chain" in resp.json().get("detail", "").lower()


def test_purchase_tx_hash_unique_constraint(monkeypatch, tmp_path):
    _client, _mod = _bootstrap(monkeypatch, tmp_path, db_name="unique-tx.db")

    from db.models import Purchase
    from db.session import get_session

    shared_tx = "0x" + "e" * 64
    with next(get_session()) as session:
        session.add(
            Purchase(
                purchase_id="p-one",
                quote_id="q-one",
                buyer_address=BUYER,
                asset="ETH",
                amount_paid=1,
                tx_hash=shared_tx,
                status="confirmed",
            )
        )
        session.commit()
        session.add(
            Purchase(
                purchase_id="p-two",
                quote_id="q-two",
                buyer_address=BUYER,
                asset="ETH",
                amount_paid=1,
                tx_hash=shared_tx,
                status="confirmed",
            )
        )
        with pytest.raises(Exception) as excinfo:
            session.commit()
        assert "unique" in str(excinfo.value).lower()


def test_public_endpoints_rate_limited(monkeypatch, tmp_path):
    monkeypatch.setenv("BUY_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("BUY_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("BUY_RATE_LIMIT_WINDOW_SECONDS", "60")
    client, mod = _bootstrap(
        monkeypatch, tmp_path, db_name="rate-limit.db", rate_limits=True
    )

    import apps.broker_api.public_rate_limit as prl

    monkeypatch.setattr(prl, "_limiter", None, raising=False)

    statuses = [
        client.get("/v1/quotes", params={"units": 1, "asset": "ETH"}).status_code
        for _ in range(3)
    ]
    assert statuses[0] == 200
    assert statuses[1] == 200
    assert statuses[2] == 429
