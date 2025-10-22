from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from libs.agentkit_ext.actions import VVVActions
from libs.agentkit_ext.agentkit_wallet import get_address


@dataclass
class StakingService:
    """Wrapper around staking actions (replace with on-chain calls)."""

    actions: VVVActions

    def approve(self, amount: int) -> Dict[str, Any]:
        return self.actions.approve(amount)

    def stake(self, amount: int) -> Dict[str, Any]:
        return self.actions.stake(amount)

    def claim(self) -> Dict[str, Any]:
        return self.actions.claim()

    def estimate_claim_cost(self) -> Optional[Dict[str, Any]]:
        estimator = getattr(self.actions, "estimate_claim_cost", None)
        if estimator is None:
            return None
        try:
            return estimator()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"claim_cost_estimate_failed:{exc}") from exc

    def unstake(self, amount: int) -> Dict[str, Any]:
        return self.actions.unstake(amount)

    def status(self) -> Dict[str, Any]:
        """Best-effort read of staking status without relying on full ABI.

        Tries common patterns:
        - balanceOf(address) for staked balance
        - earned(address) / claimable(address) / pendingRewards(address) for rewards
        Returns zeros if functions are unavailable.
        """
        from web3 import Web3  # type: ignore

        w3 = self.actions.w3
        staking_addr = Web3.to_checksum_address(self.actions.staking.address)  # type: ignore[attr-defined]
        wallet = Web3.to_checksum_address(get_address())

        def _encode_call(sig: str, args: list[bytes]) -> bytes:
            selector = Web3.keccak(text=sig)[:4]
            data = selector + b"".join(arg.rjust(32, b"\x00") for arg in args)
            return data

        def _call_int(data: bytes) -> int:
            try:
                out = w3.eth.call({"to": staking_addr, "data": data, "from": wallet})
                # Normalize bytes -> int
                if isinstance(out, (bytes, bytearray)):
                    return int.from_bytes(out, byteorder="big")
                if isinstance(out, str):  # hexstring
                    return int(out, 16)
            except Exception:
                return 0
            return 0

        addr_arg = bytes.fromhex(wallet[2:])
        # Try common reward fn names
        staked = _call_int(_encode_call("balanceOf(address)", [addr_arg]))
        rewards = 0
        for sig in [
            "earned(address)",
            "claimable(address)",
            "pendingRewards(address)",
            "rewards(address)",
        ]:
            rewards = _call_int(_encode_call(sig, [addr_arg]))
            if rewards:
                break

        cooldown_seconds = int(os.getenv("VVV_COOLDOWN_SECONDS", str(7 * 24 * 60 * 60)))

        def _call_timestamp(signatures: list[str]) -> Optional[int]:
            for sig in signatures:
                try:
                    ts = _call_int(_encode_call(sig, [addr_arg]))
                    if ts and ts > 0:
                        # Guard against obviously invalid outputs (e.g., struct packing)
                        # Accept values that look like unix timestamps within +/- 10 years.
                        if 0 < ts < 10_000_000_000:
                            return ts
                except Exception:
                    continue
            return None

        cooldown_end = _call_timestamp([
            "cooldownEndsAt(address)",
            "withdrawableTimestamp(address)",
            "cooldowns(address)",
        ])
        now = int(time.time())
        cooldown_remaining = None
        if cooldown_end and cooldown_end > now:
            cooldown_remaining = cooldown_end - now

        min_active = int(os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0") or 0)
        active_staker = bool(staked > max(0, min_active))

        return {
            "status": "ok",
            "chain_id": w3.eth.chain_id,
            "wallet": wallet,
            "staking_contract": staking_addr,
            "staked": staked,
            "rewards": rewards,
            "active_staker": active_staker,
            "min_active_stake": min_active,
            "cooldown": {
                "configured_seconds": cooldown_seconds,
                "ends_at": cooldown_end,
                "seconds_remaining": cooldown_remaining,
            },
        }

    def is_active_staker(self, status: Optional[Dict[str, Any]] = None) -> bool:
        """Return True when staking position qualifies as active for Venice rewards."""

        snapshot = status or self.status()
        if snapshot.get("status") != "ok":
            return False
        staked = int(snapshot.get("staked") or 0)
        min_active = int(snapshot.get("min_active_stake") or os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0") or 0)
        return staked > max(0, int(min_active))
