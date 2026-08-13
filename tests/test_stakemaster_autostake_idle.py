from unittest.mock import Mock

from agents.stake_master.agent import StakeMaster


def _base_status():
    return {
        "status": "ok",
        "staked": 0,
        "rewards": 0,
        "active_staker": True,
        "min_active_stake": 0,
        "cooldown": {},
    }


def test_idle_stake_calls_approve_and_stake_with_buffer_and_cap(monkeypatch):
    staking = Mock()
    staking.status.return_value = _base_status()
    staking.approve = Mock(return_value={"status": "ok"})
    staking.stake = Mock(return_value={"status": "ok"})

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "20")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "50")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")

    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 200)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(StakeMaster, "_vvv_price_usd", lambda self: 1.0)

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["executed"] is True
    assert idle["stakeable_units"] == 180
    assert idle["max_per_cycle_units"] == 50
    staking.approve.assert_called_once_with(50)
    staking.stake.assert_called_once_with(50)


def test_idle_stake_skips_when_below_buffer(monkeypatch):
    staking = Mock()
    staking.status.return_value = _base_status()
    staking.approve = Mock()
    staking.stake = Mock()

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "15")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "100")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")

    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 10)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(StakeMaster, "_vvv_price_usd", lambda self: 1.0)

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["attempted"] is False
    assert idle["executed"] is False
    assert idle["reason"] == "below_buffer"
    staking.approve.assert_not_called()
    staking.stake.assert_not_called()


def test_idle_stake_skips_when_below_min_usd(monkeypatch):
    staking = Mock()
    staking.status.return_value = _base_status()
    staking.approve = Mock()
    staking.stake = Mock()

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "0")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "100")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "10")

    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 5)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(StakeMaster, "_vvv_price_usd", lambda self: 0.5)

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["attempted"] is False
    assert idle["executed"] is False
    assert idle["reason"] == "below_min_usd"
    staking.approve.assert_not_called()
    staking.stake.assert_not_called()


def test_idle_stake_overflow_backs_off_and_retries(monkeypatch):
    staking = Mock()
    staking.status.return_value = _base_status()
    staking.approve = Mock(return_value={"status": "ok"})
    staking.stake = Mock(
        side_effect=[
            Exception("stake_estimate_failed:Panic error 0x11"),
            {"status": "ok"},
        ]
    )

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "20")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "50")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "2")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")

    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 200)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(StakeMaster, "_vvv_price_usd", lambda self: 1.0)

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["executed"] is True
    assert idle["staked_units"] == 25
    assert idle["stake_units_requested"] == 50
    assert idle["stake_overflow_retries"] == 1
    assert idle["stake_overflow_backoff_mult"] == 0.5
    assert idle["stake_overflow_attempts"] == [
        {"units": 50, "error": "stake_estimate_failed:Panic error 0x11"}
    ]
    assert staking.stake.call_count == 2
    staking.stake.assert_any_call(50)
    staking.stake.assert_any_call(25)


def test_idle_stake_overflow_stops_when_backoff_below_min_units(monkeypatch):
    staking = Mock()
    staking.status.return_value = _base_status()
    staking.approve = Mock(return_value={"status": "ok"})
    staking.stake = Mock(
        side_effect=Exception("stake_estimate_failed:Panic error 0x11")
    )

    master = StakeMaster(staking=staking)
    monkeypatch.setenv("STAKEMASTER_HEARTBEAT_DISABLE", "1")
    monkeypatch.setenv("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", "true")
    monkeypatch.setenv("VVV_DECIMALS", "0")
    monkeypatch.setenv("STAKEMASTER_WALLET_VVV_BUFFER_UNITS", "20")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", "50")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_MIN_USD", "0")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "2")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS", "30")

    monkeypatch.setattr(StakeMaster, "_wallet_vvv_balance", lambda self: 200)
    monkeypatch.setattr(
        StakeMaster, "_nonce_state", lambda self: {"latest": 0, "pending": 0}
    )
    monkeypatch.setattr(StakeMaster, "_vvv_price_usd", lambda self: 1.0)

    result = master.run_once(live=True)
    idle = result["idle_stake_action"]

    assert idle["executed"] is False
    assert idle["reason"] == "contract_overflow"
    assert idle["stake_units_requested"] == 50
    assert idle["stake_overflow_retries"] == 1
    assert idle["stake_overflow_stop_reason"] == "below_min_units"
    assert idle["stake_overflow_attempts"] == [
        {"units": 50, "error": "stake_estimate_failed:Panic error 0x11"}
    ]
    staking.stake.assert_called_once_with(50)
