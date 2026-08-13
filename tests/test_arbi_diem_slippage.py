from __future__ import annotations

from importlib import import_module


def test_arbi_diem_rejects_on_slippage(monkeypatch):
    # Arrange DIEMService with aggregator best_quote returning poor price
    qmod = import_module("libs.dex.providers")

    class FakeQuote(qmod.Quote):  # type: ignore[type-arg]
        pass

    class FakeAgg:
        def best_quote(self, amount_in, path):
            # amount_out implies exec price well below market
            return qmod.Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(amount_in // 2),
                path=path,
            )  # type: ignore[arg-type]

    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")

    # Patch DIEMService._path_from_env to avoid env
    def fake_path(self):
        return ["0xdiem", "0xusdc"]

    monkeypatch.setattr(svc_mod.DIEMService, "_path_from_env", fake_path, raising=True)

    # Patch out decimals lookups
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)
    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True
    )

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    # Market price 1.0; exec preview ~0.5 -> 5000 bps slippage; cap default 150 -> reject
    did = agent.evaluate_and_maybe_mint(
        market_price=1.0, mint_rate=1.0, desired_units=10**18
    )
    assert did is False


def test_arbi_diem_holds_without_quotes(monkeypatch):
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    from libs.dex.routes import make_route

    class NoQuoteAgg:
        def best_quote(self, amount_in, route):
            return None

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = make_route(route_tokens)

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    def fake_path_from_env(self):
        return list(route_plan.tokens)

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setattr(
        svc_mod.DIEMService, "_path_from_env", fake_path_from_env, raising=True
    )

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True
    )

    risk = risk_mod.RiskPolicy.from_env()
    svc = svc_mod.DIEMService(aggregator=NoQuoteAgg())
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    did = agent.evaluate_and_maybe_mint(
        market_price=10.0, mint_rate=1.0, desired_units=10**18
    )
    assert did is False
    rationale = getattr(agent, "_last_rationale", {})
    assert rationale.get("reason") == "no_liquidity_preview"


def test_arbi_diem_downsizes_trade_when_large_size_exceeds_slippage(monkeypatch):
    """Test that ArbiDiem successfully downsizes trades when initial size exceeds slippage cap."""
    qmod = import_module("libs.dex.providers")
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    from libs.dex.routes import make_route

    # Track quote calls to verify downsizing
    quote_calls = []

    class AdaptiveQuoteAgg:
        def __init__(self):
            self._last_quote_diagnostics = []

        def quote_all(self, amount, route_plan):
            # Record amount_in attempts (downsizing should reduce this).
            quote_calls.append(int(amount))
            amount_in = int(amount)
            # Large inputs have high slippage (5000 bps); small inputs acceptable (~30 bps).
            if int(amount_in) >= 100_000_000:  # >= $100 notional
                amount_out = int(int(amount_in) * 10**12 // 2)  # 50% value loss
            else:
                amount_out = int(int(amount_in) * 10**12 * 0.997)  # ~30 bps
            self._last_quote_diagnostics = [
                {
                    "provider": "uniswap_v2",
                    "status": "ok",
                    "route": list(route_plan.tokens),
                    "amount_out": int(amount_out),
                    "amount_in": int(amount_in),
                    "executable": True,
                }
            ]
            return [
                qmod.Quote(
                    provider="uniswap_v2",
                    amount_in=int(amount_in),
                    amount_out=int(amount_out),
                    route=route_plan,
                )
            ]

        def best_quote(self, amount_in, route, *, allowed_providers=None):
            quotes = self.quote_all(amount_in, route)
            return quotes[0] if quotes else None

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = make_route(route_tokens)

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    def fake_path_from_env(self):
        return list(route_plan.tokens)

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setattr(
        svc_mod.DIEMService, "_path_from_env", fake_path_from_env, raising=True
    )
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")  # 50 bps cap
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "0.1")  # Low min for test
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10")
    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True
    )

    risk = risk_mod.RiskPolicy.from_env()
    svc = svc_mod.DIEMService(
        aggregator=AdaptiveQuoteAgg(),
        market_data=type(
            "StubMarket",
            (object,),
            {"prices": lambda self, symbols: {"USDC": 1.0, "DIEM": 1.0, "VVV": 1.0}},
        )(),
    )
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    # Market price 1.0; initial size 1e18 will have high slippage
    # After downsizing, should find acceptable size
    did = agent.evaluate_and_maybe_mint(
        market_price=1.0, mint_rate=1.0, desired_units=10**18, simulate=True
    )

    # Should succeed with downsized trade
    assert did is True
    rationale = getattr(agent, "_last_rationale", {})
    assert rationale.get("decision") == "mint_sell"
    assert rationale.get("liquidity_adjusted_units") is not None
    # Verify that multiple quote calls were made (downsizing happened)
    assert len(quote_calls) > 1
    # Verify final size is smaller than initial
    final_units = rationale.get("units", 0)
    assert final_units < 10**18
    # Verify slippage is within cap
    slippage_bps = rationale.get("slippage_bps")
    assert slippage_bps is not None
    assert slippage_bps <= 50.0  # Within cap


def test_arbi_diem_holds_when_min_size_still_exceeds_slippage(monkeypatch):
    """Test that ArbiDiem holds when even minimum trade size exceeds slippage cap."""
    qmod = import_module("libs.dex.providers")
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    from libs.dex.routes import make_route

    class HighSlippageAgg:
        def __init__(self):
            self._last_quote_diagnostics = []

        def quote_all(self, amount, route_plan):
            amount_in = int(amount)
            amount_out = int(int(amount_in) * 10**12 * 0.99)  # ~100 bps
            self._last_quote_diagnostics = [
                {
                    "provider": "uniswap_v2",
                    "status": "ok",
                    "route": list(route_plan.tokens),
                    "amount_out": int(amount_out),
                    "amount_in": int(amount_in),
                    "executable": True,
                }
            ]
            return [
                qmod.Quote(
                    provider="uniswap_v2",
                    amount_in=int(amount_in),
                    amount_out=int(amount_out),
                    route=route_plan,
                )
            ]

        def best_quote(self, amount_in, route, *, allowed_providers=None):
            quotes = self.quote_all(amount_in, route)
            return quotes[0] if quotes else None

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = make_route(route_tokens)

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    def fake_path_from_env(self):
        return list(route_plan.tokens)

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setattr(
        svc_mod.DIEMService, "_path_from_env", fake_path_from_env, raising=True
    )
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")  # 50 bps cap (strict)
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "0.01")  # Very low min
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10")
    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True
    )

    risk = risk_mod.RiskPolicy.from_env()
    svc = svc_mod.DIEMService(
        aggregator=HighSlippageAgg(),
        market_data=type(
            "StubMarket",
            (object,),
            {"prices": lambda self, symbols: {"USDC": 1.0, "DIEM": 1.0, "VVV": 1.0}},
        )(),
    )
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    # Market price 1.0; all sizes will have 100 bps slippage > 50 bps cap
    did = agent.evaluate_and_maybe_mint(
        market_price=1.0, mint_rate=1.0, desired_units=10**18, simulate=True
    )

    # Should hold because slippage exceeds cap even at minimum size
    assert did is False
    rationale = getattr(agent, "_last_rationale", {})
    assert rationale.get("decision") == "hold"
    # Should be either slippage_exceeded_policy or slippage_exceeded
    reason = rationale.get("reason")
    assert reason in (
        "slippage_exceeded_policy",
        "slippage_exceeded",
        "extreme_slippage",
    )
    # Verify slippage was above cap
    slippage_bps = rationale.get("slippage_bps")
    if slippage_bps is not None:
        assert slippage_bps > 50.0  # Above cap


def test_recovery_slippage_cap_allows_small_capacity_recovery_trade(monkeypatch):
    """Recovery-only slippage can widen for small recovery trades."""
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    md_mod = import_module("services.marketdata.provider")
    pricing_mod = import_module("libs.pricing.diem")
    exec_mod = import_module("services.diem.execution")

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x111")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x222")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_LOCKED_SVVV_RATIO_CAP", "0.5")
    monkeypatch.setenv("DIEM_RECOVERY_CONVERGE_STEPS", "10")  # make per-step burn small

    monkeypatch.setenv("ARBI_DIEM_RECOVERY_MAX_SLIPPAGE_BPS", "500")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_SMALL_TRADE_USD", "5")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_PRICE_SANITY_MAX_REL_DIFF", "0.75")

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

    class FakeDiemService:
        def __init__(self) -> None:
            self.aggregator = None  # block stake path to force unlock

        def get_circulating_supply(self, ttl_s=600):
            return {"supply": 38_000}

        def _locked_svvv_for_wallet_safe(self):
            return 80 * 10**18

        def _mint_rate_svvv_per_diem_units(self):
            return 10**18

        def _can_burn_diem(self, amount: int):
            return {
                "can_burn": True,
                "locked_svvv": 80 * 10**18,
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

        def preview_trade(
            self, intent: exec_mod.ExecutionIntent
        ) -> exec_mod.ExecutionResult:
            assert intent.metadata.get("decision") == "capacity_recovery_buy_burn"
            return exec_mod.ExecutionResult(
                status=exec_mod.ExecutionStatus.SIMULATED,
                intent=intent,
                amount_in=4 * 10**6,  # $4 notional
                amount_out=int(intent.amount_base_units),
                slippage_bps=200.0,
                effective_price=1.3,
                pool_take_bps=10.0,
                diagnostics={},
            )

    svc = FakeDiemService()
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk_mod.RiskPolicy.from_env())

    monkeypatch.setattr(
        arbi,
        "_execute_capacity_recovery_buy_burn",
        lambda **_k: {"status": "simulated"},
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 100 * 10**18, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    did = arbi.evaluate_and_maybe_mint(
        market_price=1.2,
        mint_rate=1.0,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert did is True
    assert rationale.get("decision") == "capacity_recovery_buy_burn"
    assert rationale.get("recovery_slippage_cap_bps") == 500.0
    assert rationale.get("recovery_slippage_applied") is True


def test_recovery_slippage_cap_does_not_widen_normal_buy_burn(monkeypatch):
    """Recovery-only slippage cap does not affect normal arbitrage decisions."""
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    pricing_mod = import_module("libs.pricing.diem")
    qmod = import_module("libs.dex.providers")
    from libs.dex.routes import make_route

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "0.1")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_MAX_SLIPPAGE_BPS", "500")
    monkeypatch.setenv("ARBI_DIEM_RECOVERY_SMALL_TRADE_USD", "5")

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", lambda **_k: 1.2, raising=True
    )

    route_plan = make_route(["0xdiem", "0xweth", "0xusdc"])

    class StubAgg:
        _last_quote_diagnostics = []

        def best_quote(self, amount_in, route, *, allowed_providers=None):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in),
                route=route,
            )

        def best_quote_exact_out(self, amount_out, route, *, allowed_providers=None):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_out),
                amount_out=int(amount_out),
                route=route,
            )

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=None)
    monkeypatch.setattr(svc, "_trade_routes", lambda: [route_plan], raising=False)
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk_mod.RiskPolicy.from_env())

    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_preview_exec_price_buy",
        lambda self, units_out: (1.0, 200.0),
        raising=True,
    )
    monkeypatch.setattr(agent, "_trade_routes", lambda: [route_plan], raising=True)
    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 0, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    did = agent.evaluate_and_maybe_mint(
        market_price=1.0,
        mint_rate=1.0,
        desired_units=10**18,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(agent, "_last_rationale", {})

    assert did is False
    assert rationale.get("decision") == "hold"
    assert rationale.get("reason") in {"slippage_exceeded_policy", "slippage_exceeded"}


def test_preview_slippage_matches_reference(monkeypatch):
    """Ensure execution preview slippage is computed in USD space."""
    qmod = import_module("libs.dex.providers")
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    route = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        ]
    )

    class StubAgg:
        def __init__(self):
            self._last_quote_diagnostics = []

        def quote_all_exact_out(self, amount, route_plan):
            self._last_quote_diagnostics = [
                {
                    "provider": "uniswap_v3",
                    "status": "ok",
                    "route": list(route_plan.tokens),
                    "amount_out": int(amount),
                    "amount_in": 100_000_000,
                    "executable": True,
                }
            ]
            return [
                qmod.Quote(
                    provider="uniswap_v3",
                    amount_in=100_000_000,  # 100 USDC (6 decimals)
                    amount_out=int(amount),
                    route=route_plan,
                )
            ]

        def quote_all(self, amount, route_plan):
            return self.quote_all_exact_out(amount, route_plan)

    class StubMarket:
        def prices(self, symbols):
            return {"USDC": 1.0, "DIEM": 150.0, "VVV": 1.0}

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route]

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_out")
    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=StubMarket())
    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=10**18,  # 1 DIEM
        slippage_bps=300,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.slippage_bps is not None
    assert result.effective_price is not None

    # usd_in = 100 USDC ($100), usd_out = 1 DIEM ($150)
    expected_slip = abs(150.0 - 100.0) / 100.0 * 10_000.0
    assert abs(result.slippage_bps - expected_slip) < 1e-6


def test_preview_slippage_not_comparable_on_route_mismatch(monkeypatch):
    qmod = import_module("libs.dex.providers")
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    # Route ends in VVV, but intent expects DIEM.
    route = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
        ]
    )

    class StubAgg:
        def __init__(self):
            self._last_quote_diagnostics = []

        def quote_all_exact_out(self, amount, route_plan):
            return [
                qmod.Quote(
                    provider="uniswap_v2",
                    amount_in=10_000_000,
                    amount_out=int(amount),
                    route=route_plan,
                )
            ]

        def quote_all(self, amount, route_plan):
            return self.quote_all_exact_out(amount, route_plan)

    class StubMarket:
        def prices(self, symbols):
            return {"USDC": 1.0, "DIEM": 1.0, "VVV": 1.0}

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route]

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setenv("DIEM_DECIMALS", "18")

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=StubMarket())
    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=10**18,
        slippage_bps=300,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.slippage_bps is None
    assert result.diagnostics.get("slippage_sanity_not_comparable") is True


def test_arbi_diem_liquidity_aware_slippage_override_allows_small_trade(monkeypatch):
    """Small trades can widen the policy slippage cap when override is enabled."""
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    pricing_mod = import_module("libs.pricing.diem")
    exec_mod = import_module("services.diem.execution")
    qmod = import_module("libs.dex.providers")
    from libs.dex.routes import make_route

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_SLIPPAGE_OVERRIDE_ENABLE", "1")

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", lambda **_k: 1.2, raising=True
    )

    route_plan = make_route(["0xdiem", "0xweth", "0xusdc"])

    class StubAgg:
        _last_quote_diagnostics = []

        def best_quote(self, amount_in, route, *, allowed_providers=None):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in),
                route=route,
            )

        def best_quote_exact_out(self, amount_out, route, *, allowed_providers=None):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_out),
                amount_out=int(amount_out),
                route=route,
            )

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=None)
    monkeypatch.setattr(svc, "_trade_routes", lambda: [route_plan], raising=False)
    monkeypatch.setattr(
        svc,
        "preview_trade",
        lambda intent: exec_mod.ExecutionResult(
            status=exec_mod.ExecutionStatus.SIMULATED,
            intent=intent,
            slippage_bps=70.0,
            effective_price=1.0,
        ),
        raising=False,
    )

    market_stub = type(
        "MarketStub",
        (object,),
        {
            "prices": lambda self, symbols: {"VVV": 1.0},
            "price_health": lambda self, symbol: {"source": "aggregator"},
            "reserve_cap_units": lambda self, path, take_bps=None: None,
        },
    )()

    arbi = arbi_mod.ArbiDiem(
        diem=svc, risk=risk_mod.RiskPolicy.from_env(), market=market_stub
    )
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_get_bridge_route_for_buy",
        lambda self: None,
        raising=True,
    )
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_preview_exec_price_buy",
        lambda self, units_out: (1.0, 70.0),
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 0, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.0,
        mint_rate=1.0,
        desired_units=10**18,  # $1 notional
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "buy_burn"
    assert rationale.get("slippage_cap_bps") > rationale.get("slippage_cap_bps_base", 0)
    assert rationale.get("slippage_cap_adaptive", {}).get("applied") is True


def test_arbi_diem_slippage_decay_breaks_hold_streak(monkeypatch):
    """After repeated slippage holds, the agent widens cap under override mode."""
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    pricing_mod = import_module("libs.pricing.diem")
    exec_mod = import_module("services.diem.execution")
    qmod = import_module("libs.dex.providers")
    from libs.dex.routes import make_route

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "20.0")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("DIEM_SLIPPAGE_OVERRIDE_ENABLE", "1")

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", lambda **_k: 1.2, raising=True
    )

    route_plan = make_route(["0xdiem", "0xweth", "0xusdc"])

    class StubAgg:
        _last_quote_diagnostics = []

        def best_quote(self, amount_in, route, *, allowed_providers=None):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in),
                route=route,
            )

        def best_quote_exact_out(self, amount_out, route, *, allowed_providers=None):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_out),
                amount_out=int(amount_out),
                route=route,
            )

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=None)
    monkeypatch.setattr(svc, "_trade_routes", lambda: [route_plan], raising=False)
    monkeypatch.setattr(
        svc,
        "preview_trade",
        lambda intent: exec_mod.ExecutionResult(
            status=exec_mod.ExecutionStatus.SIMULATED,
            intent=intent,
            slippage_bps=70.0,
            effective_price=1.0,
        ),
        raising=False,
    )

    market_stub = type(
        "MarketStub",
        (object,),
        {
            "prices": lambda self, symbols: {"VVV": 1.0},
            "price_health": lambda self, symbol: {"source": "aggregator"},
            "reserve_cap_units": lambda self, path, take_bps=None: None,
        },
    )()

    arbi = arbi_mod.ArbiDiem(
        diem=svc, risk=risk_mod.RiskPolicy.from_env(), market=market_stub
    )
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_get_bridge_route_for_buy",
        lambda self: None,
        raising=True,
    )
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_preview_exec_price_buy",
        lambda self, units_out: (1.0, 70.0),
        raising=True,
    )

    portfolio = {
        "balances": {
            "SVVV": {"units": 0, "decimals": 18},
            "USDC": {"units": 1000 * 10**6, "decimals": 6},
            "DIEM": {"units": 0, "decimals": 18},
        }
    }

    desired_units = 20 * 10**18  # $20 notional (avoid small-trade boost)

    for _ in range(4):
        did = arbi.evaluate_and_maybe_mint(
            market_price=1.0,
            mint_rate=1.0,
            desired_units=desired_units,
            simulate=True,
            portfolio_snapshot=portfolio,
        )
        assert did is False
        assert getattr(arbi, "_last_rationale", {}).get("reason") in {
            "slippage_exceeded_policy",
            "slippage_exceeded",
        }

    fifth = arbi.evaluate_and_maybe_mint(
        market_price=1.0,
        mint_rate=1.0,
        desired_units=desired_units,
        simulate=True,
        portfolio_snapshot=portfolio,
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert fifth is True
    assert rationale.get("decision") == "buy_burn"
    assert rationale.get("slippage_cap_adaptive", {}).get("decay_hold_streak") == 4


def test_preview_slippage_not_comparable_when_decimals_unknown(monkeypatch):
    qmod = import_module("libs.dex.providers")
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    token_in = "0x1111111111111111111111111111111111111111"
    token_out = "0x2222222222222222222222222222222222222222"
    route = make_route([token_in, token_out])

    class StubAgg:
        def __init__(self):
            self._last_quote_diagnostics = []

        def quote_all(self, amount, route_plan):
            return [
                qmod.Quote(
                    provider="uniswap_v2",
                    amount_in=int(amount),
                    amount_out=int(amount),
                    route=route_plan,
                )
            ]

    class StubMarket:
        def prices(self, symbols):
            return {str(s): 1.0 for s in symbols}

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route]

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_erc20_contract_for",
        lambda self, addr: None,
        raising=True,
    )

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=StubMarket())
    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.SELL,
        token_in=token_in,
        token_out=token_out,
        amount_base_units=10**18,
        slippage_bps=300,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.slippage_bps is None
    assert result.diagnostics.get("slippage_sanity_not_comparable") is True
