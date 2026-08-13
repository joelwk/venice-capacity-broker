import time
from unittest.mock import Mock

from agents.stake_master.agent import StakeMaster


def _base_env(monkeypatch):
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_COMPOUND_ENABLE", "false")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "false")
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS", "0")
    # Keep tests explicit about USD floors so defaults can evolve safely.
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_USD", "0")


def test_claim_skips_below_min_value_usd(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_USD", "0")
    monkeypatch.setenv("STAKEMASTER_CLAIM_GAS_BUFFER_MULT", "2.0")

    status_snapshot = {
        "status": "ok",
        "staked": 100,
        "rewards": 10,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.claim = Mock(return_value={"status": "ok", "tx_hash": "0x1"})

    master = StakeMaster(staking=staking)
    monkeypatch.setattr(
        StakeMaster,
        "_reward_value_summary",
        lambda self, r: {"usd": 0.5, "eth": 0.00025, "eth_price_usd": 2000.0},
    )
    monkeypatch.setattr(
        StakeMaster,
        "_estimate_claim_cost",
        lambda self: {"fee_usd": 1.0, "fee_eth": 0.0005},
    )

    result = master.run_once(live=True)

    assert result["claim"]["executed"] is False
    assert result["claim"]["attempted"] is False
    assert result["claim"]["reason"] == "below_min_value_usd"
    staking.claim.assert_not_called()


def test_claim_skips_below_min_interval(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS", "60")

    status_snapshot = {
        "status": "ok",
        "staked": 100,
        "rewards": 10,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.claim = Mock(return_value={"status": "ok", "tx_hash": "0x1"})

    master = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, "_last_claim_ts", lambda self: time.time())
    monkeypatch.setattr(
        StakeMaster,
        "_reward_value_summary",
        lambda self, r: {"usd": 10.0, "eth": 0.005, "eth_price_usd": 2000.0},
    )
    monkeypatch.setattr(
        StakeMaster,
        "_estimate_claim_cost",
        lambda self: {"fee_usd": 0.1, "fee_eth": 0.00005},
    )

    result = master.run_once(live=True)

    assert result["claim"]["executed"] is False
    assert result["claim"]["attempted"] is False
    assert result["claim"]["reason"] == "below_min_interval"
    staking.claim.assert_not_called()


def test_claim_skips_when_gas_estimate_unavailable(monkeypatch):
    _base_env(monkeypatch)

    status_snapshot = {
        "status": "ok",
        "staked": 100,
        "rewards": 10,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.claim = Mock(return_value={"status": "ok", "tx_hash": "0x1"})

    master = StakeMaster(staking=staking)
    monkeypatch.setattr(
        StakeMaster,
        "_reward_value_summary",
        lambda self, r: {"usd": 10.0, "eth": 0.005, "eth_price_usd": 2000.0},
    )
    monkeypatch.setattr(StakeMaster, "_estimate_claim_cost", lambda self: None)

    result = master.run_once(live=True)

    assert result["claim"]["executed"] is False
    assert result["claim"]["attempted"] is True
    assert result["claim"]["reason"] == "gas_estimate_unavailable"
    staking.claim.assert_not_called()


def test_min_units_is_secondary_guard_only_when_usd_missing(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_UNITS", "1000")

    status_snapshot = {
        "status": "ok",
        "staked": 100,
        "rewards": 10,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.claim = Mock(return_value={"status": "ok", "tx_hash": "0x1"})

    # USD present: min-units should not dominate the decision.
    master_usd = StakeMaster(staking=staking)
    monkeypatch.setattr(
        StakeMaster,
        "_reward_value_summary",
        lambda self, r: {"usd": 0.01, "eth": 0.000005, "eth_price_usd": 2000.0},
    )
    monkeypatch.setattr(StakeMaster, "_estimate_claim_cost", lambda self: None)

    result_usd = master_usd.run_once(live=True)
    assert result_usd["claim"]["reason"] == "gas_estimate_unavailable"

    # USD missing: min-units becomes the fallback guard.
    master_units = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, "_reward_value_summary", lambda self, r: {})
    monkeypatch.setattr(
        StakeMaster,
        "_estimate_claim_cost",
        lambda self: {"fee_usd": 0.1, "fee_eth": 0.00005},
    )

    result_units = master_units.run_once(live=True)
    assert result_units["claim"]["reason"] == "below_min_units"
    staking.claim.assert_not_called()
