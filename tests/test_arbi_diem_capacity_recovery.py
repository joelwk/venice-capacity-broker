from __future__ import annotations

from importlib import import_module

import pytest

from libs.dex.providers import Quote
from services.diem.execution import (
    ExecutionIntent,
    ExecutionResult,
    ExecutionStatus,
    TradeSide,
)


def _patch_market_and_pricing(monkeypatch) -> None:
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


class _FakeAggregator:
    _execution_provider_names = ["uniswap_v2"]

    def best_quote_exact_out(self, amount_out: int, route, *, allowed_providers=None):
        # 1.0 USDC per 1.0 VVV (USDC=6d, VVV=18d).
        amount_in = int((int(amount_out) + 10**12 - 1) // 10**12)
        return Quote(
            provider="uniswap_v2",
            amount_in=int(amount_in),
            amount_out=int(amount_out),
            route=route,
        )

    def best_quote(self, amount_in: int, route, *, allowed_providers=None):
        amount_out = int(amount_in) * 10**12
        return Quote(
            provider="uniswap_v2",
            amount_in=int(amount_in),
            amount_out=int(amount_out),
            route=route,
        )


class _FakeAggregatorLowOut:
    _execution_provider_names = ["uniswap_v2"]

    def best_quote(self, amount_in: int, route, *, allowed_providers=None):
        amount_out = int(amount_in) * 10**10
        return Quote(
            provider="uniswap_v2",
            amount_in=int(amount_in),
            amount_out=int(amount_out),
            route=route,
        )


def test_capacity_recovery_triggers_buy_burn_when_locked_ratio_exceeds_cap(
    monkeypatch, caplog
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.calls = []
            self.aggregator = None

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            # 1 sVVV per 1 DIEM token (18d)
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            # Simple $1.2 per DIEM cost model for exact-out buys.
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.2 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(
            self,
            *,
            diem_amount,
            slippage_bps,
            pool_take_bps,
            simulate,
            portfolio_snapshot,
        ):
            self.calls.append(
                {
                    "diem_amount": int(diem_amount),
                    "simulate": bool(simulate),
                    "slippage_bps": int(slippage_bps),
                    "pool_take_bps": pool_take_bps,
                }
            )
            status = "simulated" if simulate else "submitted"
            return {
                "status": status,
                "buy": {"status": status},
                "burn": {"status": status},
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

    with caplog.at_level("INFO", logger="agent.arbi_diem"):
        decision = arbi.evaluate_and_maybe_mint(
            market_price=1.2,
            mint_rate=1.0,
            simulate=True,
            portfolio_snapshot=portfolio,
        )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_buy_burn"
    assert rationale.get("reason") == "locked_ratio_exceeds_cap"
    # Simulation should plan recovery but not execute it.
    assert arbi.diem.calls == []
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "unlock"
    # total=100, locked=80, cap=0.5 => excess locked=30; converge over 5 steps => burn 6 DIEM
    assert pending.get("diem_amount_units") == 6 * 10**18
    campaign_attempts = [
        r for r in caplog.records if r.message == "recovery_campaign_attempt"
    ]
    assert campaign_attempts, "expected recovery_campaign_attempt structured log event"
    last = campaign_attempts[-1]
    assert getattr(last, "event", None) == "recovery_campaign_attempt"
    assert getattr(last, "stage", None) == "execute"
    assert getattr(last, "selected_option", None) == "unlock"


def test_capacity_recovery_preferred_action_stake_overrides_unlock_economics(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_PREFERRED_ACTION", "stake")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 0.5 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
            )

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls: dict[str, int] = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["stake_usdc_units"] = int(usdc_amount_units)
        return {"status": "simulated"}

    def fake_buy_burn(
        *,
        diem_amount_units,
        slippage_bps,
        pool_take_bps,
        corr_id,
        simulate,
        portfolio_snapshot,
    ):
        raise AssertionError(
            "buy/burn should not be chosen when preferred_action=stake converges"
        )

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )
    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_burn",
        fake_buy_burn,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=10.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert (rationale.get("capacity_recovery") or {}).get("preferred_action") == "stake"
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"
    assert pending.get("usdc_amount_units") == 12 * 10**6


def test_capacity_recovery_preferred_action_stake_falls_back_to_unlock_when_not_convergent(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_PREFERRED_ACTION", "stake")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregatorLowOut()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.0 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
            )

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls: dict[str, int] = {}

    def fake_buy_burn(
        *,
        diem_amount_units,
        slippage_bps,
        pool_take_bps,
        corr_id,
        simulate,
        portfolio_snapshot,
    ):
        calls["burn_diem_units"] = int(diem_amount_units)
        return {"status": "simulated"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_burn",
        fake_buy_burn,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=10.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_buy_burn"
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "unlock"
    assert pending.get("diem_amount_units") == 6 * 10**18


def test_capacity_recovery_fallback_to_stake_when_unlock_blocked_by_no_quotes(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            return {
                "can_burn": locked >= int(amount),
                "locked_svvv": locked,
                "required_svvv": int(amount),
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                intent=intent,
                amount_in=0,
                amount_out=int(intent.amount_base_units),
                diagnostics={
                    "failure_classification": "no_executable_quotes",
                    "best_provider": "uniswap_v2",
                },
            )

        def wallet_first_buy_and_burn(
            self,
            *,
            diem_amount,
            slippage_bps,
            pool_take_bps,
            simulate,
            portfolio_snapshot,
        ):
            raise AssertionError("buy/burn should not be executed when fallback fires")

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls: dict[str, int] = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_units"] = int(usdc_amount_units)
        return {"status": "simulated"}

    def fake_buy_burn(
        *,
        diem_amount_units,
        slippage_bps,
        pool_take_bps,
        corr_id,
        simulate,
        portfolio_snapshot,
    ):
        raise AssertionError("buy/burn should not be chosen when fallback triggers")

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )
    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_burn",
        fake_buy_burn,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert (
        rationale.get("capacity_recovery_unlock_fallback_reason")
        == "no_executable_quotes"
    )
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"
    assert int(pending.get("usdc_amount_units") or 0) > 0


def test_capacity_recovery_unlock_revert_streak_triggers_stake_fallback(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_UNLOCK_REVERT_FALLBACK_STREAK", "2")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            return {
                "can_burn": locked >= int(amount),
                "locked_svvv": locked,
                "required_svvv": int(amount),
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(intent.amount_base_units) // 2,
                amount_out=int(intent.amount_base_units),
                diagnostics={
                    "best_provider": "uniswap_v2",
                    "aggregator_diagnostics": [
                        {"status": "error", "revert_reason": "fake_revert"}
                    ],
                },
            )

        def wallet_first_buy_and_burn(
            self,
            *,
            diem_amount,
            slippage_bps,
            pool_take_bps,
            simulate,
            portfolio_snapshot,
        ):
            raise AssertionError("buy/burn should not be invoked in this scenario")

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls: dict[str, int] = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls.setdefault("calls", 0)
        calls["calls"] += 1
        calls["usdc_units"] = int(usdc_amount_units)
        return {"status": "simulated"}

    def fake_buy_burn(
        *,
        diem_amount_units,
        slippage_bps,
        pool_take_bps,
        corr_id,
        simulate,
        portfolio_snapshot,
    ):
        raise AssertionError(
            "buy/burn should not be chosen when revert fallback applies"
        )

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )
    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_burn",
        fake_buy_burn,
        raising=True,
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
    assert arbi._capacity_recovery_unlock_revert_streak == 1

    second_decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert second_decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert (
        rationale.get("capacity_recovery_unlock_fallback_reason")
        == "unlock_route_revert_streak"
    )
    assert arbi._capacity_recovery_unlock_revert_streak == 2
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"


def test_capacity_recovery_triggers_when_can_mint_true_and_locked_ratio_exceeds_cap(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv(
        "DIEM_LOCKED_SVVV_RATIO_MIN_TOTAL_SVVV_UNITS",
        "50000000000000000000",  # 50 sVVV
    )

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.calls = []
            self.aggregator = None

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": True,
                "reason": "sufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 200,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.2 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(
            self,
            *,
            diem_amount,
            slippage_bps,
            pool_take_bps,
            simulate,
            portfolio_snapshot,
        ):
            self.calls.append(
                {
                    "diem_amount": int(diem_amount),
                    "simulate": bool(simulate),
                    "slippage_bps": int(slippage_bps),
                    "pool_take_bps": pool_take_bps,
                }
            )
            status = "simulated" if simulate else "submitted"
            return {
                "status": status,
                "buy": {"status": status},
                "burn": {"status": status},
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

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_buy_burn"
    assert rationale.get("reason") == "locked_ratio_exceeds_cap"
    assert arbi.diem.calls == []
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "unlock"
    assert pending.get("diem_amount_units") == 6 * 10**18


def test_capacity_recovery_prefers_stake_when_vvv_discounted_to_intrinsic(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    # Make intrinsic FV > price (vvv price is 1.0 in tests).
    monkeypatch.setenv("VVV_FV_HORIZON_DAYS", "1")
    monkeypatch.setenv("VVV_FV_DISCOUNT_APY", "0.0")
    monkeypatch.setenv("VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV", "2.0")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

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

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError(
                "buy/burn should not be chosen when stake path succeeds"
            )

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_amount_units"] = int(usdc_amount_units)
        calls["slippage_bps"] = int(slippage_bps)
        calls["simulate"] = bool(simulate)
        return {"status": "simulated"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert rationale.get("reason") == "locked_ratio_exceeds_cap"
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"
    # total=100, locked=80, cap=0.5 => stake_needed=60; converge over 5 steps => ~12 VVV
    assert pending.get("usdc_amount_units") == 12 * 10**6


def test_capacity_recovery_chooses_cheapest_effective_option_when_not_discounted(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            # Make unlock path expensive ($3 per DIEM) so stake is cheaper.
            if intent.side != TradeSide.BUY:
                raise AssertionError("test only supports BUY previews")
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 3.0 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError("buy/burn should not be chosen when stake is cheaper")

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_amount_units"] = int(usdc_amount_units)
        calls["simulate"] = bool(simulate)
        return {"status": "simulated"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"
    assert pending.get("usdc_amount_units") == 12 * 10**6


def test_capacity_recovery_prefers_stake_when_burn_economics_rich(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_RECOVERY_BURN_PREMIUM_MULT", "1.15")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "required_svvv": int(amount),
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            # Rich DIEM: $1.30 per DIEM.
            usdc_in_units = int(diem_tokens * 1.30 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError("should not choose buy+burn when DIEM is rich")

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_amount_units"] = int(usdc_amount_units)
        calls["simulate"] = bool(simulate)
        return {"status": "simulated"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.30,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert (
        rationale.get("capacity_recovery", {})
        .get("recovery_economics", {})
        .get("prefer")
        == "stake"
    )
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"
    assert pending.get("usdc_amount_units") == 12 * 10**6


def test_capacity_recovery_falls_back_to_stake_when_mint_rate_missing(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "required_svvv": int(amount),
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.10 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError("should not choose buy+burn when mint_rate is missing")

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_amount_units"] = int(usdc_amount_units)
        calls["simulate"] = bool(simulate)
        return {"status": "submitted"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.20,
        mint_rate=0.0,
        simulate=False,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert (
        rationale.get("capacity_recovery", {})
        .get("recovery_economics", {})
        .get("prefer")
        is None
    )
    assert calls["usdc_amount_units"] == 12 * 10**6
    assert calls["simulate"] is False


def test_capacity_recovery_chooses_unlock_when_unlock_is_cheaper(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.calls = []
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "required_svvv": int(amount),
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.1 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(
            self,
            *,
            diem_amount,
            slippage_bps,
            pool_take_bps,
            simulate,
            portfolio_snapshot,
        ):
            self.calls.append(int(diem_amount))
            return {"status": "simulated"}

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

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.1,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_buy_burn"
    assert (
        rationale.get("capacity_recovery", {})
        .get("recovery_economics", {})
        .get("prefer")
        == "unlock"
    )
    assert arbi.diem.calls == []
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "unlock"
    assert pending.get("diem_amount_units") == 6 * 10**18


def test_capacity_recovery_hysteresis_target_prevents_retrigger(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_TARGET", "0.45")
    monkeypatch.setenv("DIEM_RECOVERY_CONVERGE_STEPS", "5")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_BYPASS_INTERVAL", "1")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.calls = []
            self.locked_units = 80 * 10**18
            self.aggregator = None

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return int(self.locked_units)

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.2 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, *, diem_amount, **_kwargs):
            self.calls.append(int(diem_amount))
            self.locked_units = max(0, int(self.locked_units) - int(diem_amount))
            return {"status": "submitted"}

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

    for _ in range(5):
        decision = arbi.evaluate_and_maybe_mint(
            market_price=1.2,
            mint_rate=1.0,
            simulate=False,
            portfolio_snapshot=portfolio,
        )
        assert decision is True

    assert arbi.diem.calls == [7 * 10**18] * 5
    assert arbi.diem.locked_units == 45 * 10**18

    second = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=False,
        portfolio_snapshot=portfolio,
    )
    assert second is False
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("decision") == "hold"
    assert rationale.get("svvv_lock_state", {}).get("locked_ratio") == pytest.approx(
        0.45
    )


def test_capacity_recovery_falls_back_to_stake_when_unlock_preview_rejected(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            return ExecutionResult(
                status=ExecutionStatus.REJECTED,
                intent=intent,
                error="No executable quotes available at requested size",
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError(
                "buy/burn should not be chosen when preview is rejected"
            )

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_amount_units"] = int(usdc_amount_units)
        calls["simulate"] = bool(simulate)
        return {"status": "simulated"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert calls == {}
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "stake"
    assert pending.get("usdc_amount_units") == 12 * 10**6


def test_capacity_recovery_live_prefers_stake_when_unlock_preview_extreme_slippage(
    monkeypatch,
):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.65")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_TARGET", "0.50")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = _FakeAggregator()

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            # 69% locked ratio (cap=65%, target=50%).
            return 69 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "required_svvv": int(amount),
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.1 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                slippage_bps=9900.0,
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError(
                "buy/burn should not be chosen when unlock preview is pathological"
            )

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    calls = {}

    def fake_buy_vvv_and_stake(*, usdc_amount_units, slippage_bps, corr_id, simulate):
        calls["usdc_amount_units"] = int(usdc_amount_units)
        calls["simulate"] = bool(simulate)
        return {"status": "submitted"}

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_vvv_and_stake",
        fake_buy_vvv_and_stake,
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.1,
        mint_rate=1.0,
        simulate=False,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "capacity_recovery_stake"
    assert calls["usdc_amount_units"] > 0
    assert calls["simulate"] is False


def test_capacity_recovery_respects_max_trade_usd_with_step_down(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_RECOVERY_MAX_TRADE_USD", "5")
    monkeypatch.setenv("DIEM_RECOVERY_MAX_STEPS", "10")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.calls = []
            self.aggregator = None

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            locked = int(self._locked_svvv_for_wallet_safe())
            required = int(amount)
            return {
                "can_burn": locked >= required,
                "locked_svvv": locked,
                "required_svvv": required,
                "mint_rate": 10**18,
                "reason": "sufficient_locked_svvv"
                if locked >= required
                else "insufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.2 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(
            self,
            *,
            diem_amount,
            slippage_bps,
            pool_take_bps,
            simulate,
            portfolio_snapshot,
        ):
            self.calls.append(int(diem_amount))
            return {"status": "simulated"}

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

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    assert decision is True
    # burn target is 6 DIEM; max trade cap forces 6 -> 3 (first <= $5 at $1.2/DIEM)
    assert arbi.diem.calls == []
    pending = getattr(arbi, "_pending_recovery_action", None)
    assert isinstance(pending, dict) and pending
    assert pending.get("kind") == "unlock"
    assert pending.get("diem_amount_units") == 3 * 10**18


def test_capacity_recovery_blocks_when_unlock_below_min_trade(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_RECOVERY_MIN_TRADE_USD", "50")

    _patch_market_and_pricing(monkeypatch)

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        def __init__(self) -> None:
            self.calls = []
            self.aggregator = None

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "required_svvv": int(amount),
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.2 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError("should not execute when below recovery min trade")

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

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})
    assert decision is False
    assert (
        rationale.get("capacity_recovery", {})
        .get("unlock_option", {})
        .get("blocked_reason")
        == "unlock_below_min_trade"
    )


def test_capacity_recovery_respects_max_steps(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_RECOVERY_MAX_TRADE_USD", "5")
    monkeypatch.setenv("DIEM_RECOVERY_MAX_STEPS", "1")

    _patch_market_and_pricing(monkeypatch)

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

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "required_svvv": int(amount),
                "reason": "sufficient_locked_svvv",
            }

        def _can_mint(self, _diem_amount: int):
            return {
                "can_mint": False,
                "reason": "insufficient_svvv",
                "required_svvv": 100,
                "available_svvv": 0,
            }

        def preview_trade(self, intent: ExecutionIntent) -> ExecutionResult:
            diem_tokens = float(intent.amount_base_units) / 10**18
            usdc_in_units = int(diem_tokens * 1.2 * 10**6)
            return ExecutionResult(
                status=ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=int(usdc_in_units),
                amount_out=int(intent.amount_base_units),
                diagnostics={"best_provider": "uniswap_v2"},
            )

        def wallet_first_buy_and_burn(self, **_kwargs):
            raise AssertionError("should not execute when max_steps prevents step-down")

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

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})
    assert decision is False
    assert (
        rationale.get("capacity_recovery", {})
        .get("unlock_option", {})
        .get("blocked_reason")
        == "unlock_over_max_trade"
    )


def test_capacity_recovery_does_not_swap_when_stake_precheck_unfeasible(monkeypatch):
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "2")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS", "1")

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    actions_mod = import_module("libs.agentkit_ext.actions")

    class _Balance:
        units = 0

    balance = _Balance()

    class FakeActions:
        def __init__(self) -> None:
            self.w3 = type(
                "_W3",
                (),
                {
                    "eth": type(
                        "_Eth",
                        (),
                        {
                            "wait_for_transaction_receipt": lambda *_a, **_k: {
                                "status": 1
                            }
                        },
                    )()
                },
            )()

        def balance_of(self) -> int:
            return int(balance.units)

        def approve(self, amount: int):
            return {"status": "sent", "tx_hash": "0x" + "a" * 64}

        def estimate_stake(self, amount: int):
            raise RuntimeError("panic error 0x11")

        def stake(self, amount: int, *, gas_overrides=None):
            raise RuntimeError("panic error 0x11")

    fake_actions = FakeActions()
    monkeypatch.setattr(actions_mod, "VVVActions", lambda: fake_actions, raising=True)

    class FakeAggregator:
        def __init__(self) -> None:
            self.trade_calls = 0

        def best_quote(self, amount_in: int, route, *, allowed_providers=None):
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(25 * 10**18),
                route=route,
            )

        def trade_best(self, *_a, **_k):
            self.trade_calls += 1
            raise AssertionError("swap should not execute when stake precheck fails")

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = FakeAggregator()

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    res = arbi._execute_capacity_recovery_buy_vvv_and_stake(
        usdc_amount_units=10 * 10**6,
        slippage_bps=0,
        corr_id="corr",
        simulate=False,
    )

    assert res["status"] == "error"
    assert str(res.get("error") or "").startswith("stake_precheck_unfeasible:")
    assert arbi.diem.aggregator.trade_calls == 0


def test_capacity_recovery_stakes_downsized_units_after_overflow(monkeypatch):
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("ARBI_DIEM_CAPACITY_RECOVERY_SWAP_RECEIPT_TIMEOUT_S", "1")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "6")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS", "1")

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    actions_mod = import_module("libs.agentkit_ext.actions")

    class _Balance:
        units = 0

    balance = _Balance()

    class FakeActions:
        def __init__(self) -> None:
            self.approve_calls: list[int] = []
            self.estimate_calls: list[int] = []
            self.stake_calls: list[int] = []
            self.w3 = type(
                "_W3",
                (),
                {
                    "eth": type(
                        "_Eth",
                        (),
                        {
                            "wait_for_transaction_receipt": lambda *_a, **_k: {
                                "status": 1
                            }
                        },
                    )()
                },
            )()

        def balance_of(self) -> int:
            return int(balance.units)

        def approve(self, amount: int):
            self.approve_calls.append(int(amount))
            return {"status": "sent", "tx_hash": "0x" + "a" * 64}

        def estimate_stake(self, amount: int):
            self.estimate_calls.append(int(amount))
            if int(amount) > int(12 * 10**18):
                raise RuntimeError("panic error 0x11")
            return {"status": "ok"}

        def stake(self, amount: int, *, gas_overrides=None):
            self.stake_calls.append(int(amount))
            if int(amount) > int(12 * 10**18):
                raise RuntimeError("panic error 0x11")
            return {"status": "sent", "tx_hash": "0x" + "b" * 64}

    fake_actions = FakeActions()
    monkeypatch.setattr(actions_mod, "VVVActions", lambda: fake_actions, raising=True)

    class FakeAggregator:
        def __init__(self) -> None:
            self.trade_calls = 0

        def best_quote(self, amount_in: int, route, *, allowed_providers=None):
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(24 * 10**18),
                route=route,
            )

        def trade_best(self, *_a, **_k):
            self.trade_calls += 1
            balance.units += int(24 * 10**18)
            return {"tx_hash": "0x" + "c" * 64, "provider": "uniswap_v2"}

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = FakeAggregator()

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    res = arbi._execute_capacity_recovery_buy_vvv_and_stake(
        usdc_amount_units=10 * 10**6,
        slippage_bps=0,
        corr_id="corr",
        simulate=False,
    )

    assert res["status"] == "submitted"
    assert res["vvv_stake_requested_units"] == int(12 * 10**18)
    assert res["vvv_stake_submitted_units"] == int(12 * 10**18)
    assert fake_actions.approve_calls == [int(12 * 10**18)]
    assert fake_actions.stake_calls == [int(12 * 10**18)]
    assert arbi.diem.aggregator.trade_calls == 1


def test_capacity_recovery_partial_failure_logs_and_skips_repeat_buys(
    monkeypatch, caplog
):
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("ARBI_DIEM_CAPACITY_RECOVERY_SWAP_RECEIPT_TIMEOUT_S", "1")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "1")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS", "1")

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    actions_mod = import_module("libs.agentkit_ext.actions")

    class _Balance:
        units = 0

    balance = _Balance()

    class FakeActions:
        def __init__(self) -> None:
            self.w3 = type(
                "_W3",
                (),
                {
                    "eth": type(
                        "_Eth",
                        (),
                        {
                            "wait_for_transaction_receipt": lambda *_a, **_k: {
                                "status": 1
                            }
                        },
                    )()
                },
            )()

        def balance_of(self) -> int:
            return int(balance.units)

        def approve(self, amount: int):
            return {"status": "sent", "tx_hash": "0x" + "a" * 64}

        def estimate_stake(self, amount: int):
            return {"status": "ok"}

        def stake(self, amount: int, *, gas_overrides=None):
            raise RuntimeError("panic error 0x11")

    fake_actions = FakeActions()
    monkeypatch.setattr(actions_mod, "VVVActions", lambda: fake_actions, raising=True)

    class FakeAggregator:
        def __init__(self) -> None:
            self.trade_calls = 0

        def best_quote(self, amount_in: int, route, *, allowed_providers=None):
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(25 * 10**18),
                route=route,
            )

        def trade_best(self, *_a, **_k):
            self.trade_calls += 1
            balance.units += int(25 * 10**18)
            return {"tx_hash": "0x" + "c" * 64, "provider": "uniswap_v2"}

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = FakeAggregator()

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    with caplog.at_level("WARNING", logger="agent.arbi_diem"):
        res1 = arbi._execute_capacity_recovery_buy_vvv_and_stake(
            usdc_amount_units=10 * 10**6,
            slippage_bps=0,
            corr_id="corr",
            simulate=False,
        )

    assert res1["status"] == "partial_failure"
    partials = [
        r for r in caplog.records if r.message == "capacity_recovery_partial_failure"
    ]
    assert partials, "expected structured capacity_recovery_partial_failure log event"
    last = partials[-1]
    assert getattr(last, "event", None) == "capacity_recovery_partial_failure"
    assert getattr(last, "swap_tx_hash", None) == "0x" + "c" * 64
    assert getattr(last, "remaining_vvv_units", None) == int(25 * 10**18)

    res2 = arbi._execute_capacity_recovery_buy_vvv_and_stake(
        usdc_amount_units=10 * 10**6,
        slippage_bps=0,
        corr_id="corr",
        simulate=False,
    )

    assert res2["action"] == "stake_existing_vvv"
    assert res2["swap"]["status"] == "skipped"
    assert arbi.diem.aggregator.trade_calls == 1


def test_capacity_recovery_respects_max_stake_per_cycle(monkeypatch):
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("ARBI_DIEM_CAPACITY_RECOVERY_SWAP_RECEIPT_TIMEOUT_S", "1")
    monkeypatch.setenv(
        "STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", str(int(10 * 10**18))
    )

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    actions_mod = import_module("libs.agentkit_ext.actions")

    class _Balance:
        units = 0

    balance = _Balance()

    class FakeActions:
        def __init__(self) -> None:
            self.approve_calls: list[int] = []
            self.stake_calls: list[int] = []
            self.w3 = type(
                "_W3",
                (),
                {
                    "eth": type(
                        "_Eth",
                        (),
                        {
                            "wait_for_transaction_receipt": lambda *_a, **_k: {
                                "status": 1
                            }
                        },
                    )()
                },
            )()

        def balance_of(self) -> int:
            return int(balance.units)

        def approve(self, amount: int):
            self.approve_calls.append(int(amount))
            return {"status": "sent", "tx_hash": "0x" + "a" * 64}

        def estimate_stake(self, amount: int):
            return {"status": "ok"}

        def stake(self, amount: int, *, gas_overrides=None):
            self.stake_calls.append(int(amount))
            return {"status": "sent", "tx_hash": "0x" + "b" * 64}

    fake_actions = FakeActions()
    monkeypatch.setattr(actions_mod, "VVVActions", lambda: fake_actions, raising=True)

    class FakeAggregator:
        def best_quote(self, amount_in: int, route, *, allowed_providers=None):
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(24 * 10**18),
                route=route,
            )

        def trade_best(self, *_a, **_k):
            balance.units += int(24 * 10**18)
            return {"tx_hash": "0x" + "c" * 64, "provider": "uniswap_v2"}

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = FakeAggregator()

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    res = arbi._execute_capacity_recovery_buy_vvv_and_stake(
        usdc_amount_units=10 * 10**6,
        slippage_bps=0,
        corr_id="corr",
        simulate=False,
    )

    assert res["status"] == "submitted"
    assert res["vvv_stake_requested_units"] == int(10 * 10**18)
    assert res["vvv_stake_submitted_units"] == int(10 * 10**18)
    assert fake_actions.approve_calls == [int(10 * 10**18)]
    assert fake_actions.stake_calls == [int(10 * 10**18)]


def test_capacity_recovery_respects_wallet_vvv_buffer(monkeypatch):
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("ARBI_DIEM_CAPACITY_RECOVERY_SWAP_RECEIPT_TIMEOUT_S", "1")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", str(int(5 * 10**18)))

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    actions_mod = import_module("libs.agentkit_ext.actions")

    class _Balance:
        units = 0

    balance = _Balance()

    class FakeActions:
        def __init__(self) -> None:
            self.approve_calls: list[int] = []
            self.stake_calls: list[int] = []
            self.w3 = type(
                "_W3",
                (),
                {
                    "eth": type(
                        "_Eth",
                        (),
                        {
                            "wait_for_transaction_receipt": lambda *_a, **_k: {
                                "status": 1
                            }
                        },
                    )()
                },
            )()

        def balance_of(self) -> int:
            return int(balance.units)

        def approve(self, amount: int):
            self.approve_calls.append(int(amount))
            return {"status": "sent", "tx_hash": "0x" + "a" * 64}

        def estimate_stake(self, amount: int):
            return {"status": "ok"}

        def stake(self, amount: int, *, gas_overrides=None):
            self.stake_calls.append(int(amount))
            return {"status": "sent", "tx_hash": "0x" + "b" * 64}

    fake_actions = FakeActions()
    monkeypatch.setattr(actions_mod, "VVVActions", lambda: fake_actions, raising=True)

    class FakeAggregator:
        def best_quote(self, amount_in: int, route, *, allowed_providers=None):
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(24 * 10**18),
                route=route,
            )

        def trade_best(self, *_a, **_k):
            balance.units += int(24 * 10**18)
            return {"tx_hash": "0x" + "c" * 64, "provider": "uniswap_v2"}

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = FakeAggregator()

    arbi = arbi_mod.ArbiDiem(
        diem=FakeDiemService(), risk=risk_mod.RiskPolicy.from_env()
    )

    res = arbi._execute_capacity_recovery_buy_vvv_and_stake(
        usdc_amount_units=10 * 10**6,
        slippage_bps=0,
        corr_id="corr",
        simulate=False,
    )

    assert res["status"] == "submitted"
    assert res["vvv_stake_requested_units"] == int(19 * 10**18)
    assert res["vvv_stake_submitted_units"] == int(19 * 10**18)
    assert fake_actions.approve_calls == [int(19 * 10**18)]
    assert fake_actions.stake_calls == [int(19 * 10**18)]
