from __future__ import annotations

from importlib import import_module


def test_capacity_recovery_stake_sizing_moves_ratio_bps(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_RECOVERY_CONVERGE_STEPS", "5")

    md_mod = import_module("services.marketdata.provider")
    pricing_mod = import_module("libs.pricing.diem")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )
    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", lambda **_k: 1.0, raising=True
    )

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = None

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )

    rationale = getattr(arbi, "_last_rationale", {})
    recovery = (rationale.get("capacity_recovery") or {}).copy()

    assert recovery.get("stake_units_target") == 12 * 10**18
    assert recovery.get("converge_steps_remaining") == 5

    locked_units = 80 * 10**18
    total_units = 100 * 10**18
    stake_units = int(recovery.get("stake_units_target") or 0)

    before_bps = int(locked_units * 10_000 // total_units)
    after_bps = int(locked_units * 10_000 // (total_units + stake_units))
    improvement_bps = before_bps - after_bps

    assert improvement_bps >= 500
