Run uv run pytest -q
.......F.....................................s...FF..................... [ 49%]
................ss........................F..............F..........Treasury wallet portfolio snapshot:
  address: <unavailable>
  balances: <unavailable>
  notes:
    - resolve address: No module named 'coinbase_agentkit'
    - treasury address unavailable
.....s                                                                   [100%]
=================================== FAILURES ===================================
_____________ test_arbi_diem_includes_portfolio_caps_in_rationale ______________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f18be0c91d0>

    def test_arbi_diem_includes_portfolio_caps_in_rationale(monkeypatch):
        """Test that ArbiDiem includes portfolio caps and telemetry in rationale."""
        monkeypatch.setenv("RISK_ENABLE_PORTFOLIO_CAP", "1")
    
        mock_aggregator = MagicMock()
        mock_diem = DIEMService(aggregator=mock_aggregator)
        arbi = ArbiDiem(diem=mock_diem, risk=RiskPolicy.from_env())
    
        mock_market = MagicMock()
        mock_market.prices.return_value = {"DIEM": 2.0, "VVV": 1.0, "USDC": 1.0}
        arbi.market = mock_market
    
        arbi.evaluate_and_maybe_mint(
            market_price=2.5,  # Premium over fair value
            mint_rate=1.0,
            current_inventory_usd=1000.0,
            utilization_ratio=0.5,
            simulate=True,
        )
    
        rationale = arbi._last_rationale
        assert rationale is not None
        assert "current_inventory_usd" in rationale
        assert rationale["current_inventory_usd"] == 1000.0
        assert "desired_units" in rationale
        assert "suggested_units" in rationale
>       assert "portfolioAdjustedUnits" in rationale
E       AssertionError: assert 'portfolioAdjustedUnits' in {'current_inventory_usd': 1000.0, 'decision': 'hold', 'desired_units': 1000000000000000000000, 'discount_mult': 1.05, ...}

tests/test_arbi_diem_portfolio_cap.py:38: AssertionError
----------------------------- Captured stdout call -----------------------------
2025-11-05 14:34:36 | INFO | AGENT[arbi_diem] | Market px=2.5000, fair/day=5.4834
2025-11-05 14:34:38 | INFO | marketdata.provider | dynamic trade path: tokens=['0xf4d97f2da56e8c3098f3a8d538db630a2606a024', '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'] fees=None
2025-11-05 14:35:42 | INFO | marketdata.provider | trade path verified
2025-11-05 14:36:22 | INFO | marketdata.provider | trade path verified
2025-11-05 14:36:40 | WARNING | marketdata.provider | trade path verification empty
2025-11-05 14:36:53 | INFO | AGENT[arbi_diem] | Buy/burn skipped: no exact-out preview available
------------------------------ Captured log call -------------------------------
INFO     agent.arbi_diem:agent.py:336 Market px=2.5000, fair/day=5.4834
INFO     marketdata.provider:dynamic_paths.py:361 dynamic trade path: tokens=['0xf4d97f2da56e8c3098f3a8d538db630a2606a024', '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'] fees=None
INFO     marketdata.provider:provider.py:777 trade path verified
INFO     marketdata.provider:provider.py:777 trade path verified
WARNING  marketdata.provider:provider.py:779 trade path verification empty
INFO     agent.arbi_diem:agent.py:542 Buy/burn skipped: no exact-out preview available
___________________ test_broker_pricing_stepwise_adjustment ____________________

    def test_broker_pricing_stepwise_adjustment():
        """Test that broker pricing adjusts stepwise."""
        os.environ["BROKER_UTIL_TARGET"] = "0.65"
        os.environ["BROKER_PRICE_STEP_BPS"] = "50"
        os.environ["BROKER_BASE_PRICE_USD"] = "1.0"
    
        mock_keys = MagicMock(spec=KeyManager)
        broker = CapacityBroker(keys=mock_keys)
        broker._last_price = 1.0
    
        # High utilization should increase price
        pricing, _ = broker._derive_inventory_policy(0.80)
        assert pricing is not None
>       assert pricing["mode"] == "surge"
E       AssertionError: assert 'normal' == 'surge'
E         
E         - surge
E         + normal

tests/test_broker_pricing_loop.py:53: AssertionError
_______________________ test_broker_tracks_price_history _______________________

    def test_broker_tracks_price_history():
        """Test that broker tracks price history for rollback."""
        mock_keys = MagicMock(spec=KeyManager)
        broker = CapacityBroker(keys=mock_keys)
    
        # Run multiple cycles
        for util in [0.70, 0.75, 0.65, 0.60]:
>           summary = broker.run_once(parent_key="test_key", enforce_limits=False)
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests/test_broker_pricing_loop.py:71: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
agents/capacity_broker/agent.py:39: in run_once
    client = self.keys.client
             ^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <MagicMock spec='KeyManager' id='139744225393104'>, name = 'client'

    def __getattr__(self, name):
        if name in {'_mock_methods', '_mock_unsafe'}:
            raise AttributeError(name)
        elif self._mock_methods is not None:
            if name not in self._mock_methods or name in _all_magics:
>               raise AttributeError("Mock object has no attribute %r" % name)
E               AttributeError: Mock object has no attribute 'client'

/opt/hostedtoolcache/Python/3.11.14/x64/lib/python3.11/unittest/mock.py:653: AttributeError
_________________________ test_recycle_profits_dry_run _________________________

    def test_recycle_profits_dry_run():
        """Test profit recycling in dry-run mode."""
        mock_aggregator = MagicMock()
        mock_quote = MagicMock()
        mock_quote.amount_out = 1_000_000_000_000_000_000  # 1 VVV
        mock_aggregator.best_quote.return_value = mock_quote
    
        mock_stake_master = MagicMock()
    
        usdc_wei = 1_000_000  # 1 USDC (6 decimals)
    
        result = recycle_profits_to_stake(
            amount_usdc_wei=usdc_wei,
            aggregator=mock_aggregator,
            stake_master=mock_stake_master,
            dry_run=True,
        )
    
>       assert result["status"] == "dry_run"
E       AssertionError: assert 'skipped' == 'dry_run'
E         
E         - dry_run
E         + skipped

tests/test_profit_recycling.py:28: AssertionError
____________________ test_single_loop_quorum_blocks_actions ____________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7f18bcacd210>

    def test_single_loop_quorum_blocks_actions(monkeypatch):
        orch_mod = import_module("graph.workflows.orchestrator")
        risk_mod = import_module("services.risk.policy")
    
        class FakeArbi:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.risk = risk_mod.RiskPolicy.from_env()
                self._last_rationale = {"decision": "mint_sell"}
    
            def evaluate_and_maybe_mint(
                self,
                price: float,
                mint_rate: float = 1.0,
                desired_units=None,
                current_inventory_usd=None,
                simulate: bool | None = None,
                **kwargs,
            ) -> bool:
                self.calls.append({"simulate": simulate})
                self._last_rationale = {"decision": "mint_sell"}
                return True
    
        class FakeQuorum:
            def __init__(self, allow: bool) -> None:
                self.allow = allow
                self.calls = 0
    
            def decide(self) -> bool:  # noqa: D401
                self.calls += 1
                return self.allow
    
        class FakeStake:
            def run_once(self, live: bool = False):  # noqa: D401
                return {"status": "ok", "live": live}
    
        class FakeCapacity:
            def run_once(self, parent_key: str | None = None, enforce_limits: bool = True):  # noqa: D401
                return {"status": "ok"}
    
        class FakeMarket:
            def unified_signals(self, ttl_s: int = 30):  # noqa: D401
                return {}
    
            def prices(self, symbols):  # noqa: ANN001, D401
                return {"DIEM": 1.0, "VVV": 0.2, "USDC": 1.0}
    
            def diem_mint_rate(self, ttl_s: int = 60):  # noqa: D401
                return {"tokens_per_diem": 1.0, "source": "test"}
    
        arbi = FakeArbi()
        quorum = FakeQuorum(allow=False)
        orchestrator = orch_mod.SingleLoopOrchestrator(
            stake_master=FakeStake(),
            arbi=arbi,
            capacity_broker=FakeCapacity(),
            market=FakeMarket(),
            quorum=quorum,
        )
    
        record = orchestrator.run_cycle(dry_run=False, enable_live=True)
    
        assert quorum.calls == 1
        # simulate True should be recorded, but no simulate False call when blocked
        assert len(arbi.calls) == 1 and arbi.calls[0]["simulate"] is True
        assert record["arbi"]["execution"]["status"] == "blocked"
        # Verify portfolio inventory and broker utilization are persisted
        if "portfolio" in record:
            assert "inventoryUsd" in record["portfolio"]
            assert "perAssetUsd" in record["portfolio"]
        if "brokerUtilization" in record:
            assert isinstance(record["brokerUtilization"], (float, type(None)))
    
        # Verify ArbiDiem rationale includes portfolio telemetry
        if "arbi" in record and "why" in record["arbi"]:
            rationale = record["arbi"]["why"]
            if isinstance(rationale, dict):
>               assert "desired_units" in rationale or "suggested_units" in rationale
E               AssertionError: assert ('desired_units' in {'decision': 'mint_sell'} or 'suggested_units' in {'decision': 'mint_sell'})

tests/test_single_loop_orchestrator.py:160: AssertionError
----------------------------- Captured stdout call -----------------------------
2025-11-05 14:40:56 | INFO | WORKFLOW[orchestrator] | single-loop cycle: {'ts': 1762353656.9186623, 'stake': {'status': 'ok', 'live': False}, 'arbi': {'agent': 'arbi_diem', 'action': 'mint_sell', 'price': 1.0, 'inventoryUsd': 2.0, 'dry_run': False, 'correlationId': 'fff2fcf3-2eab-415a-ad1d-2f4e080d96dd', 'ts': 1762353656.9186623, 'mintRate': 1.0, 'mintRateSource': 'test', 'limits': {'slippage_bps_cap': 150, 'max_trade_usd': 50.0, 'max_inventory_usd': 100000.0, 'max_trade_units': 0}, 'signals': {'utilization_ratio': None, 'vol_bps': 0.0, 'utilization_vol_bps': None}, 'outcome': False, 'why': {'decision': 'mint_sell'}, 'execution': {'status': 'blocked', 'executed': False}, 'signalDecision': True, 'quorum': {'status': 'blocked'}}, 'capacity': {'status': 'ok'}, 'brokerUtilization': None, 'agents': {'stake_master': {'status': 'ok', 'live': False}, 'arbi_diem': {'agent': 'arbi_diem', 'action': 'mint_sell', 'price': 1.0, 'inventoryUsd': 2.0, 'dry_run': False, 'correlationId': 'fff2fcf3-2eab-415a-ad1d-2f4e080d96dd', 'ts': 1762353656.9186623, 'mintRate': 1.0, 'mintRateSource': 'test', 'limits': {'slippage_bps_cap': 150, 'max_trade_usd': 50.0, 'max_inventory_usd': 100000.0, 'max_trade_units': 0}, 'signals': {'utilization_ratio': None, 'vol_bps': 0.0, 'utilization_vol_bps': None}, 'outcome': False, 'why': {'decision': 'mint_sell'}, 'execution': {'status': 'blocked', 'executed': False}, 'signalDecision': True, 'quorum': {'status': 'blocked'}}, 'capacity_broker': {'status': 'ok'}}, 'reflex': None, 'progressive': {'requested': False, 'override': False, 'live': True, 'state': None}}
------------------------------ Captured log call -------------------------------
INFO     workflow.orchestrator:orchestrator.py:1378 single-loop cycle: {'ts': 1762353656.9186623, 'stake': {'status': 'ok', 'live': False}, 'arbi': {'agent': 'arbi_diem', 'action': 'mint_sell', 'price': 1.0, 'inventoryUsd': 2.0, 'dry_run': False, 'correlationId': 'fff2fcf3-2eab-415a-ad1d-2f4e080d96dd', 'ts': 1762353656.9186623, 'mintRate': 1.0, 'mintRateSource': 'test', 'limits': {'slippage_bps_cap': 150, 'max_trade_usd': 50.0, 'max_inventory_usd': 100000.0, 'max_trade_units': 0}, 'signals': {'utilization_ratio': None, 'vol_bps': 0.0, 'utilization_vol_bps': None}, 'outcome': False, 'why': {'decision': 'mint_sell'}, 'execution': {'status': 'blocked', 'executed': False}, 'signalDecision': True, 'quorum': {'status': 'blocked'}}, 'capacity': {'status': 'ok'}, 'brokerUtilization': None, 'agents': {'stake_master': {'status': 'ok', 'live': False}, 'arbi_diem': {'agent': 'arbi_diem', 'action': 'mint_sell', 'price': 1.0, 'inventoryUsd': 2.0, 'dry_run': False, 'correlationId': 'fff2fcf3-2eab-415a-ad1d-2f4e080d96dd', 'ts': 1762353656.9186623, 'mintRate': 1.0, 'mintRateSource': 'test', 'limits': {'slippage_bps_cap': 150, 'max_trade_usd': 50.0, 'max_inventory_usd': 100000.0, 'max_trade_units': 0}, 'signals': {'utilization_ratio': None, 'vol_bps': 0.0, 'utilization_vol_bps': None}, 'outcome': False, 'why': {'decision': 'mint_sell'}, 'execution': {'status': 'blocked', 'executed': False}, 'signalDecision': True, 'quorum': {'status': 'blocked'}}, 'capacity_broker': {'status': 'ok'}}, 'reflex': None, 'progressive': {'requested': False, 'override': False, 'live': True, 'state': None}}
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/websockets/legacy/__init__.py:6
  /home/runner/work/venice-capacity-broker/venice-capacity-broker/.venv/lib/python3.11/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

tests/ui/test_control_plane_buy_flow.py:123
  /home/runner/work/venice-capacity-broker/venice-capacity-broker/tests/ui/test_control_plane_buy_flow.py:123: PytestUnknownMarkWarning: Unknown pytest.mark.e2e - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.e2e

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_arbi_diem_portfolio_cap.py::test_arbi_diem_includes_portfolio_caps_in_rationale - AssertionError: assert 'portfolioAdjustedUnits' in {'current_inventory_usd': 1000.0, 'decision': 'hold', 'desired_units': 1000000000000000000000, 'discount_mult': 1.05, ...}
FAILED tests/test_broker_pricing_loop.py::test_broker_pricing_stepwise_adjustment - AssertionError: assert 'normal' == 'surge'
  
  - surge
  + normal
FAILED tests/test_broker_pricing_loop.py::test_broker_tracks_price_history - AttributeError: Mock object has no attribute 'client'
FAILED tests/test_profit_recycling.py::test_recycle_profits_dry_run - AssertionError: assert 'skipped' == 'dry_run'
  
  - dry_run
  + skipped
FAILED tests/test_single_loop_orchestrator.py::test_single_loop_quorum_blocks_actions - AssertionError: assert ('desired_units' in {'decision': 'mint_sell'} or 'suggested_units' in {'decision': 'mint_sell'})
5 failed, 137 passed, 5 skipped, 2 warnings in 384.65s (0:06:24)