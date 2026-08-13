from unittest.mock import Mock

from agents.stake_master.agent import StakeMaster


def test_autostake_skips_when_balance_insufficient(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 0,
        "rewards": 0,
        "active_staker": False,
        "min_active_stake": 100,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock()
    staking.stake = Mock()
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_PROGRESSIVE_ENABLE", "true")
    master = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 50)

    result = master.run_once(live=True)

    stake_action = result["stake_action"]
    assert stake_action["reason"] == "insufficient_balance"
    assert stake_action["attempted"] is False
    assert stake_action["executed"] is False
    assert staking.approve.called is False
    assert staking.stake.called is False


def test_autostake_overflow_backs_off_and_retries(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 0,
        "rewards": 0,
        "active_staker": False,
        "min_active_stake": 100,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock(return_value={"status": "ok"})
    staking.stake = Mock(
        side_effect=[
            Exception("stake_estimate_failed:Panic error 0x11"),
            {"status": "ok", "tx_hash": "0xabc"},
        ]
    )

    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_PROGRESSIVE_ENABLE", "true")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "2")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")

    master = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 200)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )

    result = master.run_once(live=True)

    stake_action = result["stake_action"]
    assert stake_action["executed"] is True
    assert stake_action["reason"] == "auto_stake_backoff"
    assert stake_action["staked_units"] == 50
    staking.approve.assert_called_once_with(100)
    assert staking.stake.call_count == 2
    staking.stake.assert_any_call(100)
    staking.stake.assert_any_call(50)


def test_idle_stake_respects_buffer_and_cap(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 100,
        "rewards": 0,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock(return_value={"tx": "approve"})
    staking.stake = Mock(return_value={"status": "ok", "tx_hash": "0xabc"})

    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "10")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "60")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")

    master = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 200)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["executed"] is True
    assert idle["reason"] == "idle_stake"
    assert idle["buffer_units"] == 10
    assert idle["stakeable_units"] == 190
    assert idle["max_per_cycle_units"] == 60
    staking.approve.assert_called_once_with(60)
    staking.stake.assert_called_once_with(60)


def test_idle_stake_skips_on_pending_nonce(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 50,
        "rewards": 0,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock()
    staking.stake = Mock()

    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "0")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "100")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")

    master = StakeMaster(staking=staking)
    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 50)
    monkeypatch.setattr(
        StakeMaster,
        "_nonce_state",
        lambda self: {"latest": 1, "pending": 2},
    )

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["attempted"] is False
    assert idle["executed"] is False
    assert idle["reason"] == "pending_nonce"
    assert staking.approve.called is False
    assert staking.stake.called is False


def test_compound_runs_after_claim(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 0,
        "rewards": 10,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock(return_value={"tx": "approve"})
    staking.stake = Mock(return_value={"status": "ok"})
    staking.claim = Mock(return_value={"status": "ok", "tx_hash": "0x1"})

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "false")
    monkeypatch.setenv("STAKEMASTER_AUTO_COMPOUND_ENABLE", "true")
    monkeypatch.setenv("STAKEMASTER_COMPOUND_ONLY_IF_CLAIMED", "true")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 50)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(
        StakeMaster, "_estimate_claim_cost", lambda self: {"fee_wei": 0}
    )
    monkeypatch.setattr(StakeMaster, "_reward_value_summary", lambda self, r: {})

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]
    assert idle["executed"] is True
    assert idle["mode"] == "compound"
    staking.approve.assert_called_once()
    staking.stake.assert_called_once()


def test_compound_skips_when_no_claim(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 0,
        "rewards": 0,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock()
    staking.stake = Mock()
    staking.claim = Mock(return_value={"status": "ok"})

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "false")
    monkeypatch.setenv("STAKEMASTER_AUTO_COMPOUND_ENABLE", "true")
    monkeypatch.setenv("STAKEMASTER_COMPOUND_ONLY_IF_CLAIMED", "true")
    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 50)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(
        StakeMaster, "_estimate_claim_cost", lambda self: {"fee_wei": 0}
    )
    monkeypatch.setattr(StakeMaster, "_reward_value_summary", lambda self, r: {})

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]
    assert idle["attempted"] is False
    assert idle["executed"] is False
    assert idle["reason"] == "disabled"
    staking.approve.assert_not_called()
    staking.stake.assert_not_called()


def test_recommendation_ingest_and_annotation(monkeypatch):
    status_snapshot = {
        "status": "ok",
        "staked": 0,
        "rewards": 0,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }
    staking = Mock()
    staking.status.return_value = status_snapshot
    staking.approve = Mock(return_value={"tx": "approve"})
    staking.stake = Mock(return_value={"status": "ok"})

    master = StakeMaster(staking=staking)
    master.ingest_recommendation({"shortfall_units": 25, "reason": "insufficient_svvv"})
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "0")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "50")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")

    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 40)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["executed"] is True
    assert idle.get("recommendation_helped") is True
    assert idle.get("recommendation_help_units") == 25
    assert result["recommendation"]["reason"] == "insufficient_svvv"


def _base_claim_env(monkeypatch):
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_COMPOUND_ENABLE", "false")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "false")
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS", "0")


def test_claim_skips_when_reward_usd_below_gas_buffered_cost(monkeypatch):
    _base_claim_env(monkeypatch)
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
        lambda self, r: {"usd": 1.0, "eth": 0.0005, "eth_price_usd": 2000.0},
    )
    monkeypatch.setattr(
        StakeMaster,
        "_estimate_claim_cost",
        lambda self: {"fee_usd": 0.6, "fee_eth": 0.0003},
    )

    result = master.run_once(live=True)
    claim = result["claim"]

    assert claim["executed"] is False
    assert claim["attempted"] is False
    assert claim["reason"] == "below_min_value_usd"
    assert claim["required_reward_usd"] == 1.2
    staking.claim.assert_not_called()


def test_claim_proceeds_when_reward_usd_meets_required_reward(monkeypatch):
    _base_claim_env(monkeypatch)
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
        lambda self, r: {"usd": 1.2, "eth": 0.0006, "eth_price_usd": 2000.0},
    )
    monkeypatch.setattr(
        StakeMaster,
        "_estimate_claim_cost",
        lambda self: {"fee_usd": 0.6, "fee_eth": 0.0003},
    )

    result = master.run_once(live=True)
    claim = result["claim"]

    assert claim["executed"] is True
    assert claim["attempted"] is True
    assert claim["reason"] == "claimed"
    assert claim["required_reward_usd"] == 1.2
    staking.claim.assert_called_once()


def test_claim_uses_units_floor_when_price_data_missing(monkeypatch):
    _base_claim_env(monkeypatch)
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_USD", "0")
    monkeypatch.setenv("STAKEMASTER_MIN_CLAIM_UNITS", "100")

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
    monkeypatch.setattr(StakeMaster, "_reward_value_summary", lambda self, r: {})
    monkeypatch.setattr(
        StakeMaster,
        "_estimate_claim_cost",
        lambda self: {"fee_usd": 0.1, "fee_eth": 0.00005},
    )

    result = master.run_once(live=True)
    claim = result["claim"]

    assert claim["executed"] is False
    assert claim["attempted"] is False
    assert claim["reason"] == "below_min_units"
    assert claim["min_units"] == 100
    assert claim["deficit_units"] == 90
    staking.claim.assert_not_called()
