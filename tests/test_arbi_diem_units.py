from decimal import Decimal
from types import SimpleNamespace

from agents.arbi_diem.agent import ArbiDiem
from services.risk.policy import RiskPolicy


def _build_agent(monkeypatch) -> ArbiDiem:
    risk = RiskPolicy.from_env()
    return ArbiDiem(diem=SimpleNamespace(), risk=risk)


def test_desired_units_scales_token_amounts(monkeypatch):
    monkeypatch.setenv("ARBI_DIEM_MINT_UNITS", "1000")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.delenv("ARBI_DIEM_MINT_UNITS_BASE_UNITS", raising=False)
    agent = _build_agent(monkeypatch)
    assert agent._desired_units() == 1000 * 10**18


def test_desired_units_handles_decimal_tokens(monkeypatch):
    monkeypatch.setenv("ARBI_DIEM_MINT_UNITS", "12.5")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.delenv("ARBI_DIEM_MINT_UNITS_BASE_UNITS", raising=False)
    agent = _build_agent(monkeypatch)
    expected = int(Decimal("12.5") * (10**18))
    assert agent._desired_units() == expected


def test_desired_units_accepts_explicit_base_units(monkeypatch):
    monkeypatch.setenv("ARBI_DIEM_MINT_UNITS", "123456789")
    monkeypatch.setenv("ARBI_DIEM_MINT_UNITS_BASE_UNITS", "true")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    agent = _build_agent(monkeypatch)
    assert agent._desired_units() == 123456789


def test_desired_units_uses_usd_notional_when_price_available(monkeypatch):
    monkeypatch.delenv("ARBI_DIEM_MINT_UNITS", raising=False)
    monkeypatch.delenv("ARBI_DIEM_MINT_UNITS_BASE_UNITS", raising=False)
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("ARBI_DIEM_TRADE_USD", "2")
    agent = _build_agent(monkeypatch)
    # Price $2 -> 2 USD should be 1 token = 1e18 base units
    assert agent._desired_units(2.0) == 1 * 10**18


def test_desired_units_respects_risk_cap(monkeypatch):
    monkeypatch.delenv("ARBI_DIEM_MINT_UNITS", raising=False)
    monkeypatch.delenv("ARBI_DIEM_MINT_UNITS_BASE_UNITS", raising=False)
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("ARBI_DIEM_TRADE_USD", "50")
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "5")
    agent = _build_agent(monkeypatch)
    # With price $1 and cap 5 USD, expect 5 tokens -> 5e18 units
    assert agent._desired_units(1.0) == 5 * 10**18
