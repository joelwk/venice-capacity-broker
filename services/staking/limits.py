from __future__ import annotations

import os
from dataclasses import dataclass

_ENV_WALLET_BUFFER_UNITS = "STAKEMASTER_WALLET_VVV_BUFFER_UNITS"
_ENV_MAX_PER_CYCLE_UNITS = "STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS"
_ENV_STAKE_INCREMENT_UNITS = "STAKEMASTER_STAKE_INCREMENT_UNITS"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(str(raw), 0)
    except Exception:
        try:
            return int(float(str(raw)))
        except Exception:
            return int(default)


@dataclass(frozen=True)
class IdleStakeLimits:
    """StakeMaster-style guardrails for staking idle VVV (base units).

    - wallet_buffer_units: keep at least this much VVV liquid in the wallet.
    - max_per_cycle_units: cap stake size per action/cycle (0 disables cap).
    - stake_increment_units: round stake amounts down to this increment (0 disables rounding).
    """

    wallet_buffer_units: int
    max_per_cycle_units: int
    stake_increment_units: int = 0

    @staticmethod
    def from_env(
        *,
        default_wallet_buffer_units: int = int(25 * 10**16),  # 0.25 VVV @ 18d
        default_max_per_cycle_units: int = int(10 * 10**18),  # 10 VVV @ 18d
        default_stake_increment_units: int = 0,  # 0 = no increment rounding
    ) -> IdleStakeLimits:
        wallet_buffer = max(
            0, _env_int(_ENV_WALLET_BUFFER_UNITS, default_wallet_buffer_units)
        )
        max_per_cycle = max(
            0, _env_int(_ENV_MAX_PER_CYCLE_UNITS, default_max_per_cycle_units)
        )
        stake_increment = max(
            0, _env_int(_ENV_STAKE_INCREMENT_UNITS, default_stake_increment_units)
        )
        return IdleStakeLimits(
            wallet_buffer_units=int(wallet_buffer),
            max_per_cycle_units=int(max_per_cycle),
            stake_increment_units=int(stake_increment),
        )

    def apply(self, *, requested_units: int, wallet_balance_units: int | None) -> int:
        units = max(0, int(requested_units))

        if wallet_balance_units is not None:
            stakeable = max(
                0, int(wallet_balance_units) - int(self.wallet_buffer_units)
            )
            units = min(int(units), int(stakeable))

        if int(self.max_per_cycle_units) > 0:
            units = min(int(units), int(self.max_per_cycle_units))

        # Round down to stake increment if configured
        if int(self.stake_increment_units) > 0:
            units = (int(units) // int(self.stake_increment_units)) * int(
                self.stake_increment_units
            )

        return max(0, int(units))


def idle_stake_limits_payload(limits: IdleStakeLimits) -> dict[str, int | None]:
    return {
        "wallet_vvv_buffer_units": int(limits.wallet_buffer_units),
        "max_per_cycle_units": (
            int(limits.max_per_cycle_units)
            if int(limits.max_per_cycle_units) > 0
            else None
        ),
    }
