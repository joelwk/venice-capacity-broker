from __future__ import annotations

import types

import pytest

from decimal import Decimal

from services.wallet.provider import (
    AgentKitWalletAdapter,
    WalletError,
    describe_treasury_portfolio,
    sweep_profits_to_cold,
    transfer_from_cold_to_hot,
)


@pytest.fixture(scope="module", autouse=True)
def _log_treasury_portfolio(request: pytest.FixtureRequest) -> None:
    """Emit a snapshot of the treasury wallet to aid config updates."""

    snapshot = describe_treasury_portfolio()
    address = snapshot.get("address")
    balances = snapshot.get("balances", {})
    errors = snapshot.get("errors", [])

    lines = ["Treasury wallet portfolio snapshot:"]
    lines.append(f"  address: {address or '<unavailable>'}")

    if balances:
        for symbol in sorted(balances.keys()):
            entry = balances[symbol] or {}
            if symbol == "ETH" and "wei" in entry:
                try:
                    human = Decimal(entry["wei"]) / Decimal(10**18)
                    lines.append(
                        f"  {symbol}: {human:.6f} ETH ({entry['wei']} wei)"
                    )
                except Exception:
                    lines.append(f"  {symbol}: {entry.get('wei', '<unknown>')} wei")
                continue

            units = entry.get("units")
            decimals = entry.get("decimals")
            token_addr = entry.get("token_address")
            if units is None or decimals is None:
                lines.append(f"  {symbol}: <unavailable>")
                continue
            try:
                human = Decimal(units) / Decimal(10**int(decimals))
                lines.append(
                    f"  {symbol}: {human:.6f} ({units} base units, decimals={decimals}, token={token_addr})"
                )
            except Exception:
                lines.append(
                    f"  {symbol}: {units} base units (decimals={decimals}, token={token_addr})"
                )
    else:
        lines.append("  balances: <unavailable>")

    if errors:
        lines.append("  notes:")
        for err in errors:
            lines.append(f"    - {err}")

    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter and hasattr(reporter, "write_line"):
        for line in lines:
            reporter.write_line(line)
    else:
        print("\n".join(lines))


class _StubProvider:
    def __init__(self) -> None:
        self._address = "0x4c4b1f1e1d1c1b1a191817161514131211100000"
        self.sent: dict | None = None

    def get_address(self) -> str:
        return self._address

    def send_transaction(self, tx: dict) -> str:
        self.sent = tx
        return "0xtxhash"


def test_adapter_normalizes_address_and_send(monkeypatch):
    stub = _StubProvider()
    adapter = AgentKitWalletAdapter(inner=stub, kind="eth")
    web3_mod = pytest.importorskip("web3")
    Web3 = web3_mod.Web3
    assert adapter.address == Web3.to_checksum_address(stub.get_address())
    tx_hash = adapter.send_transaction(to="0x4c4b1f1e1d1c1b1a191817161514131211100000", value=123)
    assert tx_hash == "0xtxhash"
    assert stub.sent is not None
    assert stub.sent["to"] == Web3.to_checksum_address(stub.get_address())


def test_adapter_sign_message_smart_wallet(monkeypatch):
    stub = _StubProvider()
    adapter = AgentKitWalletAdapter(inner=stub, kind="smart")
    acct_module = pytest.importorskip("eth_account")
    Account = acct_module.Account
    acct = Account.create()
    key_hex = acct.key.hex()
    if not key_hex.startswith("0x"):
        key_hex = "0x" + key_hex
    monkeypatch.setenv("OWNER", key_hex)
    sig = adapter.sign_message("hello world")
    sig_hex = sig[2:] if sig.startswith("0x") else sig
    int(sig_hex, 16)
    assert len(sig_hex) == 130


def test_transfer_from_cold_to_hot(monkeypatch):
    provider = _StubProvider()
    monkeypatch.setattr("services.wallet.provider.get_agentkit_wallet", lambda: (provider, "eth"))

    captured = {}

    def fake_build(w3, from_addr, to, value):  # noqa: ANN001
        captured["build"] = (from_addr, to, value)
        return {"dummy": True}

    class DummyEth:
        def __init__(self) -> None:
            self.sent = None

        def send_raw_transaction(self, raw):  # noqa: ANN001
            self.sent = raw
            return types.SimpleNamespace(hex=lambda: "0xfeed")

    class DummyWeb3:
        def __init__(self) -> None:
            self.eth = DummyEth()

    monkeypatch.setattr("services.wallet.provider.build_eip1559_tx", fake_build)
    monkeypatch.setattr("services.wallet.provider.get_web3", lambda: DummyWeb3())

    web3_mod = pytest.importorskip("web3")
    Web3 = web3_mod.Web3

    class DummySigner:
        address = "0xCc0d000000000000000000000000000000000000"

        def sign_transaction(self, tx):  # noqa: ANN001
            assert tx == {"dummy": True}
            return types.SimpleNamespace(rawTransaction=b"raw")

    result = transfer_from_cold_to_hot(10, cold_signer=DummySigner())
    assert result == "0xfeed"
    assert captured["build"] == (
        DummySigner.address,
        Web3.to_checksum_address(provider.get_address()),
        10,
    )


def test_sweep_profits_to_cold(monkeypatch):
    provider = _StubProvider()
    monkeypatch.setattr("services.wallet.provider.get_agentkit_wallet", lambda: (provider, "eth"))

    class DummyEth:
        gas_price = 0

        def get_balance(self, _addr):  # noqa: ANN001
            return 1_000_000

    class DummyWeb3:
        def __init__(self) -> None:
            self.eth = DummyEth()

    monkeypatch.setattr("services.wallet.provider.get_web3", lambda: DummyWeb3())

    pytest.importorskip("web3")
    tx_hash = sweep_profits_to_cold(
        100_000,
        cold_address="0xCc0d000000000000000000000000000000000001",
        gas_buffer_wei=50_000,
    )
    assert tx_hash == "0xtxhash"
    assert provider.sent is not None
    assert provider.sent["value"] == 1_000_000 - 100_000 - 50_000


def test_sweep_requires_balance(monkeypatch):
    provider = _StubProvider()
    monkeypatch.setattr("services.wallet.provider.get_agentkit_wallet", lambda: (provider, "eth"))

    class DummyEth:
        gas_price = 0

        def get_balance(self, _addr):  # noqa: ANN001
            return 50

    class DummyWeb3:
        def __init__(self) -> None:
            self.eth = DummyEth()

    monkeypatch.setattr("services.wallet.provider.get_web3", lambda: DummyWeb3())

    pytest.importorskip("web3")
    with pytest.raises(WalletError):
        sweep_profits_to_cold(100, cold_address="0xCc0d000000000000000000000000000000000001")
