from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import NoSuchModuleError

# Remove lightweight stubs so we can reload the real modules when available.
# Evict entire package trees: popping only the top-level package leaves stale
# submodules cached, which breaks dialect resolution on re-import.
for _mod in list(sys.modules):
    if _mod in ("db.session", "db.models") or _mod.split(".", 1)[0] in (
        "sqlmodel",
        "sqlalchemy",
    ):
        sys.modules.pop(_mod, None)

# Skip when SQLModel is unavailable (features require DB models).
pytest.importorskip("sqlmodel")


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
    import sys
    from types import ModuleType, SimpleNamespace

    _ensure_sqlite_dialect()
    sqlalc = sys.modules.get("sqlalchemy")
    if sqlalc is None or not hasattr(sqlalc, "dialects"):
        sqlalc = ModuleType("sqlalchemy")
        sqlalc.desc = lambda arg: arg  # type: ignore[attr-defined]
        sqlalc.sqlite = SimpleNamespace()  # type: ignore[attr-defined]
        sqlalc.dialects = SimpleNamespace(sqlite=SimpleNamespace())  # type: ignore[attr-defined]
        sys.modules["sqlalchemy"] = sqlalc
    os.environ.setdefault("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    os.environ.setdefault("BROKER_ADMIN_TOKEN", "test-admin")
    os.environ.setdefault("VENICE_PARENT_KEY", "parent-test")
    spec = importlib.util.spec_from_file_location(
        "broker_app_test", "apps/broker_api/app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


BASE_CHAIN_ID_HEX = "0x2105"  # 8453, matches PURCHASE_CHAIN_ID default
RECEIPT_BLOCK_HEX = hex(12345)
CURRENT_BLOCK_HEX = hex(12345 + 200)


def _rpc_responder(tx_payload: dict | None = None, receipt: dict | None = None):
    """Fake JSON-RPC covering the calls made during payment verification."""

    def _fake_rpc(urls, method, params):
        if method == "eth_chainId":
            return BASE_CHAIN_ID_HEX
        if method == "eth_blockNumber":
            return CURRENT_BLOCK_HEX
        if method == "eth_getTransactionReceipt":
            if receipt is not None:
                return receipt
            return {"status": hex(1), "blockNumber": RECEIPT_BLOCK_HEX, "logs": []}
        if method == "eth_getTransactionByHash":
            return tx_payload
        raise AssertionError(f"unexpected rpc call: {method}")

    return _fake_rpc


def _install_signature_stub(mod: ModuleType, monkeypatch, wallet: str) -> None:
    class FakeAccount:
        @staticmethod
        def recover_message(message, signature):
            return wallet

    monkeypatch.setattr(mod.purchases, "Account", FakeAccount, raising=False)
    monkeypatch.setattr(
        mod.purchases, "encode_defunct", lambda text: text, raising=False
    )


def _signed_fields(client: TestClient, tx_hash: str, wallet: str) -> dict:
    challenge = client.get(
        "/v1/purchases/challenge",
        params={"txHash": tx_hash, "buyerAddress": wallet},
    )
    assert challenge.status_code == 200, challenge.text
    return {"signature": "sig-e2e", "nonce": challenge.json()["nonce"]}


def _bootstrap_purchase(
    monkeypatch,
    tmp_path,
    *,
    issue_fn=None,
    tx_hash: str = "0x" + "a" * 64,
    db_name: str = "buyer-recover.db",
    store_name: str = "store-recover.json",
) -> tuple[TestClient, ModuleType, dict, str, str]:
    """Provision a quote + app client for recovery tests."""
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")

    db_path = tmp_path / db_name
    store_path = tmp_path / store_name
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

    mod = _load_broker_app_module()

    buyer_wallet = "0xdef0000000000000000000000000000000000002"
    tx_payload = {
        "to": "0xabc0000000000000000000000000000000000001",
        "from": buyer_wallet,
        "value": hex(0),
    }

    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _rpc_responder(tx_payload), raising=True
    )
    monkeypatch.setattr(mod.purchases, "_challenge_store", {}, raising=False)
    _install_signature_stub(mod, monkeypatch, buyer_wallet)

    if issue_fn is None:

        def _default_issue(parent_key, label, consumption_limit, expires_at=None):
            return {
                "apiKey": "sk-test-recover",
                "id": "kid-recover",
            }  # gitleaks:allow test API key

        issue_fn = _default_issue

    monkeypatch.setattr(mod.keys, "issue_scoped_key", issue_fn, raising=True)

    client = TestClient(mod.app)
    quote = client.get("/v1/quotes", params={"units": 5, "asset": "ETH"})
    assert quote.status_code == 200, quote.text
    data = quote.json()
    tx_payload["value"] = hex(int(data["totalPrice"]))
    return client, mod, data, buyer_wallet, tx_hash


def test_buyer_lifecycle_quote_verify_subkey(monkeypatch, tmp_path):
    # Enable features and static pricing for ETH
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))  # 0.001 ETH per unit
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    # Minimal chain/payment config
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    # Provide a temporary SQLite database so SQLModel path is available even when
    # Postgres/SQL_DATABASE_URL is not configured in CI environments.
    db_path = tmp_path / "buyer.db"
    store_path = tmp_path / "store.json"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

    mod = _load_broker_app_module()

    # Stub RPC calls to confirm ETH payment
    buyer_wallet = "0xdef0000000000000000000000000000000000002"
    tx_payload = {
        "to": "0xabc0000000000000000000000000000000000001",
        "from": buyer_wallet,
        "value": hex(5 * 10**15),  # placeholder; updated once quote is fetched
    }
    tx_hash = "0x" + "1" * 64

    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _rpc_responder(tx_payload), raising=True
    )
    _install_signature_stub(mod, monkeypatch, buyer_wallet)

    # Stub subkey issuance to avoid Venice network
    def _fake_issue(
        parent_key: str,
        label: str,
        consumption_limit: int,
        expires_at: str | None = None,
    ):
        return {"apiKey": "sk-test-123", "id": "kid-1"}  # gitleaks:allow test API key

    monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)

    # Use FastAPI TestClient
    client = TestClient(mod.app)
    # Get a quote for ETH
    q = client.get("/v1/quotes", params={"units": 5, "asset": "ETH"})
    assert q.status_code == 200, q.text
    data = q.json()
    tx_payload["value"] = hex(int(data["totalPrice"]))
    assert int(data["units"]) == 5 and data["asset"] == "ETH"
    assert data.get("priceHealth") is None
    assert data.get("priceGuard") is None
    # Verify purchase with a fake tx hash
    v = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": data["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert v.status_code == 200, v.text
    out = v.json()
    assert out["status"] in {"confirmed", "fulfilled"}
    # If fulfilled, subkey must be present
    if out["status"] == "fulfilled":
        assert out["subkey"] == "sk-test-123"  # gitleaks:allow test API key


def test_purchase_fractional_units_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    db_path = tmp_path / "buyer-fractional.db"
    store_path = tmp_path / "store-fractional.json"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

    mod = _load_broker_app_module()

    buyer_wallet = "0xdef0000000000000000000000000000000000002"
    tx_payload = {
        "to": "0xabc0000000000000000000000000000000000001",
        "from": buyer_wallet,
        "value": hex(10**15),
    }
    tx_hash = "0x" + "4" * 64

    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _rpc_responder(tx_payload), raising=True
    )
    _install_signature_stub(mod, monkeypatch, buyer_wallet)

    recorded: dict[str, object] = {}

    def _fake_issue(
        parent_key: str, label: str, consumption_limit, expires_at: str | None = None
    ):
        recorded["consumption_limit"] = consumption_limit
        return {
            "apiKey": "sk-fractional-123",
            "id": "kid-2",
        }  # gitleaks:allow test API key

    monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)

    client = TestClient(mod.app)
    quote = client.get("/v1/quotes", params={"units": 0.01, "asset": "ETH"})
    assert quote.status_code == 200, quote.text
    data = quote.json()
    tx_payload["value"] = hex(int(data["totalPrice"]))
    assert data.get("priceHealth") is None
    assert data.get("priceGuard") is None

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": data["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(recorded.get("consumption_limit"), dict)
    assert recorded["consumption_limit"]["diem"] == pytest.approx(
        0.01, rel=1e-9, abs=1e-12
    )


def test_budget_quote_path(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "market")
    monkeypatch.setenv("PURCHASE_UNITS_KIND", "diem")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    monkeypatch.setenv("PRICE_DISCOUNT_DEFAULT_BPS", "500")
    monkeypatch.setenv("PRICE_DISCOUNT_BPS", "500")
    monkeypatch.delenv("PRICE_DISCOUNT_DEFAULT", raising=False)
    monkeypatch.setenv("PRICING_WARMUP_TIMEOUT_SECONDS", "1")
    db_path = tmp_path / "budget.db"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    db_path.unlink(missing_ok=True)

    class DummyMDP:
        def prices(self, symbols, *args, **kwargs):
            return {"DIEM": 200.0, "ETH": 4000.0, "USDC": 1.0}

        def last_prices_stats(self):
            return {}

        def price_health(self, symbol: str, max_age: float = 180.0):
            return {
                "symbol": symbol,
                "valid": True,
                "source": "prefetch",
                "value": 200.0,
            }

    # Patch before app load: pricing warms the provider in a background
    # thread during startup, so a late patch would let real network calls out.
    import services.marketdata.provider as provider_mod

    monkeypatch.setattr(
        provider_mod,
        "MarketDataProvider",
        lambda *args, **kwargs: DummyMDP(),
        raising=True,
    )

    mod = _load_broker_app_module()

    # Force deterministic pricing without on-chain calls.
    pricing = mod.quotes._pricing
    engine = pricing.engine

    assert pricing._base_discount_fraction("ETH") == pytest.approx(0.05, rel=1e-9)

    def _fake_prices():
        # base_unit_usd, market prices
        return (200.0, {"DIEM": 200.0, "ETH": 4000.0, "USDC": 1.0})

    monkeypatch.setattr(engine, "_resolve_prices", _fake_prices, raising=True)

    client = TestClient(mod.app)
    resp = client.get("/v1/quotes", params={"budget": 10, "asset": "ETH"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["asset"] == "ETH"
    assert pytest.approx(payload["units"], rel=1e-6) == 0.05
    assert payload.get("discountBps") == 500
    eth_amount = payload["totalPrice"] / 1e18
    assert pytest.approx(eth_amount, rel=1e-6) == 0.002375
    price_health = payload.get("priceHealth")
    if price_health is not None:
        assert isinstance(price_health, dict)
        assert price_health.get("symbol") == "DIEM"
        assert isinstance(price_health.get("valid"), bool)
    price_guard = payload.get("priceGuard")
    if price_health is None:
        assert price_guard is None
    else:
        assert price_guard is None or price_guard.get("reason") == "price_guard"

    too_small = client.get("/v1/quotes", params={"budget": 0.1, "asset": "ETH"})
    assert too_small.status_code == 400
    detail = too_small.json()["detail"]
    detail_text = detail if isinstance(detail, str) else str(detail.get("message", ""))
    assert "at least" in detail_text.lower()


def test_purchase_verify_rejects_wrong_sender(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    db_path = tmp_path / "buyer-invalid.db"
    store_path = tmp_path / "store-invalid.json"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

    mod = _load_broker_app_module()

    buyer_wallet = "0xdef0000000000000000000000000000000000002"
    sender_wallet = "0xabc0000000000000000000000000000000000003"
    tx_payload = {
        "to": "0xabc0000000000000000000000000000000000001",
        "from": sender_wallet,
        "value": hex(5 * 10**15),
    }
    tx_hash = "0x" + "2" * 64

    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _rpc_responder(tx_payload), raising=True
    )
    _install_signature_stub(mod, monkeypatch, buyer_wallet)

    client = TestClient(mod.app)
    quote = client.get("/v1/quotes", params={"units": 1, "asset": "ETH"})
    assert quote.status_code == 200, quote.text
    data = quote.json()
    tx_payload["value"] = hex(int(data["totalPrice"]))
    assert data.get("priceHealth") is None
    assert data.get("priceGuard") is None

    resp = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": data["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert resp.status_code == 400
    assert "unexpected address" in resp.json().get("detail", "")


def test_purchase_verify_reuses_existing_key(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    db_path = tmp_path / "buyer-reuse.db"
    store_path = tmp_path / "store-reuse.json"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_path))
    db_path.unlink(missing_ok=True)
    store_path.unlink(missing_ok=True)

    mod = _load_broker_app_module()

    buyer_wallet = "0xdef0000000000000000000000000000000000002"
    tx_payload = {
        "to": "0xabc0000000000000000000000000000000000001",
        "from": buyer_wallet,
        "value": hex(5 * 10**15),
    }
    tx_hash = "0x" + "3" * 64

    monkeypatch.setattr(
        mod.purchases, "_rpc_call", _rpc_responder(tx_payload), raising=True
    )
    _install_signature_stub(mod, monkeypatch, buyer_wallet)

    calls: list[int] = []

    def _fake_issue(
        parent_key: str,
        label: str,
        consumption_limit: int,
        expires_at: str | None = None,
    ):
        calls.append(1)
        return {
            "apiKey": "sk-existing-123",
            "id": "kid-1",
        }  # gitleaks:allow test API key

    monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)

    client = TestClient(mod.app)
    quote = client.get("/v1/quotes", params={"units": 1, "asset": "ETH"})
    assert quote.status_code == 200, quote.text
    data = quote.json()
    tx_payload["value"] = hex(int(data["totalPrice"]))
    assert data.get("priceHealth") is None
    assert data.get("priceGuard") is None

    first = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": data["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["subkey"] == "sk-existing-123"  # gitleaks:allow test API key
    assert calls == [1]

    def _rpc_fail(*args, **kwargs):
        raise AssertionError("_rpc_call should not be invoked on replay")

    monkeypatch.setattr(mod.purchases, "_rpc_call", _rpc_fail, raising=True)

    second = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": data["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["subkey"] == "sk-existing-123"  # gitleaks:allow test API key
    assert second_body["status"] == "fulfilled"
    assert calls == [1]


def test_purchase_recover_returns_existing_key(monkeypatch, tmp_path):
    key_value = "sk-recover-existing"  # gitleaks:allow test API key

    def issue_fn(parent_key, label, consumption_limit, expires_at=None):
        return {"apiKey": key_value, "id": "kid-existing"}

    client, mod, quote, buyer_wallet, tx_hash = _bootstrap_purchase(
        monkeypatch,
        tmp_path,
        issue_fn=issue_fn,
        tx_hash="0x" + "a" * 64,
        db_name="recover-existing.db",
        store_name="recover-existing.json",
    )

    verify = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["subkey"] == key_value

    class FakeAccount:
        @staticmethod
        def recover_message(message, signature):
            assert signature == "sig-existing"
            return buyer_wallet

    monkeypatch.setattr(mod.purchases, "Account", FakeAccount, raising=False)
    monkeypatch.setattr(
        mod.purchases, "encode_defunct", lambda text: text, raising=False
    )

    challenge = client.get(
        "/v1/purchases/recover/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer_wallet},
    )
    assert challenge.status_code == 200, challenge.text
    payload = challenge.json()

    recover = client.post(
        "/v1/purchases/recover",
        json={
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            "signature": "sig-existing",
            "nonce": payload["nonce"],
        },
    )
    assert recover.status_code == 200, recover.text
    out = recover.json()
    assert out["subkey"] == key_value
    assert out["status"] == "fulfilled"


def test_purchase_recover_mints_after_failure(monkeypatch, tmp_path):
    calls: list[str] = []

    def issue_fn(parent_key, label, consumption_limit, expires_at=None):
        if not calls:
            calls.append("fail")
            raise RuntimeError("temporary issuance failure")
        calls.append("success")
        return {
            "apiKey": "sk-recover-new",
            "id": "kid-new",
        }  # gitleaks:allow test API key

    client, mod, quote, buyer_wallet, tx_hash = _bootstrap_purchase(
        monkeypatch,
        tmp_path,
        issue_fn=issue_fn,
        tx_hash="0x" + "b" * 64,
        db_name="recover-new.db",
        store_name="recover-new.json",
    )

    first = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["subkey"] is None
    assert first_body["status"] == "confirmed"
    assert calls == ["fail"]

    class FakeAccount:
        @staticmethod
        def recover_message(message, signature):
            return buyer_wallet

    monkeypatch.setattr(mod.purchases, "Account", FakeAccount, raising=False)
    monkeypatch.setattr(
        mod.purchases, "encode_defunct", lambda text: text, raising=False
    )

    challenge = client.get(
        "/v1/purchases/recover/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer_wallet},
    )
    assert challenge.status_code == 200, challenge.text
    payload = challenge.json()

    recover = client.post(
        "/v1/purchases/recover",
        json={
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            "signature": "sig-new",
            "nonce": payload["nonce"],
        },
    )
    assert recover.status_code == 200, recover.text
    out = recover.json()
    assert out["subkey"] == "sk-recover-new"  # gitleaks:allow test API key
    assert out["status"] == "fulfilled"
    assert calls == ["fail", "success"]


def test_purchase_recover_rejects_bad_signature(monkeypatch, tmp_path):
    client, mod, quote, buyer_wallet, tx_hash = _bootstrap_purchase(
        monkeypatch,
        tmp_path,
        tx_hash="0x" + "c" * 64,
        db_name="recover-bad.db",
        store_name="recover-bad.json",
    )

    verify = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": quote["quoteId"],
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            **_signed_fields(client, tx_hash, buyer_wallet),
        },
    )
    assert verify.status_code == 200, verify.text

    class FakeAccount:
        @staticmethod
        def recover_message(message, signature):
            return "0xbbb0000000000000000000000000000000000003"

    monkeypatch.setattr(mod.purchases, "Account", FakeAccount, raising=False)
    monkeypatch.setattr(
        mod.purchases, "encode_defunct", lambda text: text, raising=False
    )

    challenge = client.get(
        "/v1/purchases/recover/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer_wallet},
    )
    assert challenge.status_code == 200, challenge.text
    payload = challenge.json()

    recover = client.post(
        "/v1/purchases/recover",
        json={
            "txHash": tx_hash,
            "buyerAddress": buyer_wallet,
            "signature": "sig-invalid",
            "nonce": payload["nonce"],
        },
    )
    assert recover.status_code == 403, recover.text
    assert "signature" in recover.json().get("detail", "").lower()
