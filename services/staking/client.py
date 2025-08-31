from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

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
        staked = 0
        rewards = 0
        # balanceOf(address)
        staked = _call_int(_encode_call("balanceOf(address)", [addr_arg]))
        # Try a few reward selectors in order
        for sig in [
            "earned(address)",
            "claimable(address)",
            "pendingRewards(address)",
            "rewards(address)",
        ]:
            rewards = _call_int(_encode_call(sig, [addr_arg]))
            if rewards:
                break

        return {
            "status": "ok",
            "chain_id": w3.eth.chain_id,
            "wallet": wallet,
            "staking_contract": staking_addr,
            "staked": staked,
            "rewards": rewards,
        }
