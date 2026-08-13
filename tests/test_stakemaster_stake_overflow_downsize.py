from __future__ import annotations

from agents.stake_master.agent import StakeMaster


class _Staking:
    def __init__(self, *, succeed_at_or_below: int | None = None) -> None:
        self.calls: list[int] = []
        self._succeed_at_or_below = succeed_at_or_below

    def stake(self, amount: int):  # type: ignore[override]
        self.calls.append(int(amount))
        if self._succeed_at_or_below is not None and int(amount) <= int(
            self._succeed_at_or_below
        ):
            return {"status": "sent", "tx_hash": "0x" + "e" * 64}
        raise RuntimeError("panic error 0x11")


def test_overflow_downsizes_until_success(monkeypatch):
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "6")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS", "1")

    staking = _Staking(succeed_at_or_below=12)
    agent = StakeMaster(staking=staking)

    tx, staked_units, attempts, stop_reason = agent._stake_with_overflow_backoff(100)

    assert tx is not None
    assert stop_reason is None
    assert staked_units == 12
    assert staking.calls == [100, 50, 25, 12]
    assert all(entry["error_signature"] == "panic_0x11" for entry in attempts)


def test_overflow_stops_below_min_units(monkeypatch):
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES", "6")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT", "0.5")
    monkeypatch.setenv("STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS", "40")

    staking = _Staking(succeed_at_or_below=None)
    agent = StakeMaster(staking=staking)

    tx, staked_units, attempts, stop_reason = agent._stake_with_overflow_backoff(100)

    assert tx is None
    assert stop_reason == "below_min_units"
    assert staking.calls == [100, 50]
    assert len(attempts) == 2
