from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from libs.agentkit_ext.actions import VVVActions
from libs.agentkit_ext.agentkit_wallet import get_address

try:
    from web3 import Web3  # type: ignore
except Exception:  # pragma: no cover - web3 optional in tests
    Web3 = None  # type: ignore[assignment]


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
        if Web3 is None:
            raise RuntimeError("web3 library not available; staking status requires web3")

        w3 = self.actions.w3
        staking_addr = Web3.to_checksum_address(self.actions.staking.address)  # type: ignore[attr-defined]
        wallet = Web3.to_checksum_address(get_address())

        addr_arg = bytes.fromhex(wallet[2:])

        staked, staked_meta = self._staked_amount(wallet, staking_addr, addr_arg)
        rewards = self._reward_amount(wallet, staking_addr, addr_arg)

        cooldown_seconds = int(os.getenv("VVV_COOLDOWN_SECONDS", str(7 * 24 * 60 * 60)))

        def _call_timestamp(signatures: list[str]) -> Optional[int]:
            for sig in signatures:
                try:
                    ts = self._call_raw_uint(staking_addr, wallet, addr_arg, sig)
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
            "staked_source": staked_meta if staked_meta else None,
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

    # ------------------------------------------------------------------
    def _staked_amount(
        self,
        wallet: str,
        staking_addr: str,
        wallet_arg: bytes,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        w3 = self.actions.w3
        codec = getattr(w3, "codec", None)
        zero_candidate: Optional[Tuple[int, Dict[str, Any]]] = None

        for target in self._staked_targets(staking_addr):
            for sig, outputs, select in self._staked_call_specs():
                value = self._call_spec_uint(
                    target,
                    wallet,
                    wallet_arg,
                    sig,
                    outputs,
                    select,
                    codec,
                )
                if value is None:
                    continue
                meta = {"target": target, "signature": sig, "outputs": outputs, "select": select}
                if value > 0:
                    return int(value), meta
                if zero_candidate is None:
                    zero_candidate = (0, meta)
        if zero_candidate is not None:
            return zero_candidate
        return 0, None

    def _reward_amount(self, wallet: str, staking_addr: str, wallet_arg: bytes) -> int:
        reward_specs = os.getenv("STAKEMASTER_REWARD_SIGNATURES")
        if reward_specs:
            sigs = [entry.strip() for entry in reward_specs.split(",") if entry.strip()]
        else:
            sigs = [
                "earned(address)",
                "claimable(address)",
                "pendingRewards(address)",
                "rewards(address)",
            ]
        for sig in sigs:
            try:
                value = self._call_raw_uint(staking_addr, wallet, wallet_arg, sig)
            except Exception:
                continue
            if value:
                return value
        return 0

    def _call_raw_uint(self, target: str, wallet: str, wallet_arg: bytes, signature: str) -> Optional[int]:
        w3 = self.actions.w3
        data = self._encode_signature(signature, wallet_arg)
        if data is None:
            return None
        try:
            out = w3.eth.call({"to": target, "data": data, "from": wallet})
        except Exception:
            return None
        return self._decode_uint(out)

    @staticmethod
    def _encode_signature(signature: str, wallet_arg: bytes) -> Optional[bytes]:
        if Web3 is None:
            return None
        sig = signature.strip()
        if not sig:
            return None
        if "(address)" in sig:
            return Web3.keccak(text=sig)[:4] + wallet_arg.rjust(32, b"\x00")
        return Web3.keccak(text=sig)[:4]

    def _decode_uint(self, raw: Any) -> Optional[int]:
        if raw in (None, b"", "0x", "0x0"):
            return 0
        if isinstance(raw, str):
            payload = raw[2:] if raw.startswith("0x") else raw
            try:
                data = bytes.fromhex(payload)
            except Exception:
                return None
        else:
            data = bytes(raw)
        if not data:
            return 0
        try:
            return int.from_bytes(data[:32], byteorder="big")
        except Exception:
            return None

    def _call_spec_uint(
        self,
        target: str,
        wallet: str,
        wallet_arg: bytes,
        signature: str,
        outputs: Sequence[str],
        select: int,
        codec: Any,
    ) -> Optional[int]:
        if Web3 is None:
            return None
        sig = signature.strip()
        if not sig:
            return None
        data = Web3.keccak(text=sig)[:4]
        if "(address)" in sig:
            data += wallet_arg.rjust(32, b"\x00")
        w3 = self.actions.w3
        try:
            out = w3.eth.call({"to": target, "data": data, "from": wallet})
        except Exception:
            return None
        if out in (None, b"", "0x", "0x0"):
            return 0
        if isinstance(out, str):
            payload = out[2:] if out.startswith("0x") else out
            try:
                buf = bytes.fromhex(payload)
            except Exception:
                return None
        else:
            buf = bytes(out)
        if not buf:
            return 0
        if codec is not None:
            try:
                if len(outputs) == 1:
                    decoded = codec.decode_single(outputs[0], buf)
                else:
                    decoded_all = codec.decode_abi(list(outputs), buf)
                    idx = select if 0 <= select < len(decoded_all) else 0
                    decoded = decoded_all[idx]
            except Exception:
                decoded = None
        else:
            decoded = None
        if decoded is None:
            return self._decode_uint(buf)
        try:
            return int(decoded)
        except Exception:
            return None

    def _staked_call_specs(self) -> Sequence[Tuple[str, Sequence[str], int]]:
        raw = os.getenv("STAKEMASTER_STAKED_SIGNATURES")
        entries: Sequence[str]
        if raw:
            entries = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            entries = (
                "balanceOf(address)",
                "staked(address)",
                "stakes(address)|uint256,uint256|0",
                "userInfo(address)|uint256,uint256|0",
            )
        specs: list[Tuple[str, Sequence[str], int]] = []
        for entry in entries:
            spec = self._parse_spec_entry(entry)
            if spec is not None:
                specs.append(spec)
        if not specs:
            specs.append(("balanceOf(address)", ("uint256",), 0))
        return specs

    @staticmethod
    def _parse_spec_entry(entry: str) -> Optional[Tuple[str, Sequence[str], int]]:
        parts = entry.split("|")
        signature = parts[0].strip()
        if not signature:
            return None
        outputs: Sequence[str] = ("uint256",)
        select = 0
        if len(parts) > 1 and parts[1].strip():
            outputs = tuple(t.strip() for t in parts[1].split(",") if t.strip())
            if not outputs:
                outputs = ("uint256",)
        if len(parts) > 2 and parts[2].strip():
            try:
                select = int(parts[2])
            except Exception:
                select = 0
        return signature, outputs, select

    def _staked_targets(self, staking_addr: str) -> Sequence[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        raw = os.getenv("STAKEMASTER_STAKED_CONTRACTS")
        wallet_token = os.getenv("VVV_TOKEN_ADDRESS")
        svvv_token = os.getenv("SVVV_TOKEN_ADDRESS")

        def _append_candidate(value: Optional[str]) -> None:
            if not value or Web3 is None:
                return
            try:
                checksum = Web3.to_checksum_address(value)
            except Exception:
                return
            key = checksum.lower()
            if key in seen:
                return
            seen.add(key)
            candidates.append(checksum)

        if raw:
            for item in raw.split(","):
                key = item.strip().lower()
                if not key:
                    continue
                if key == "staking":
                    _append_candidate(staking_addr)
                elif key == "token":
                    _append_candidate(wallet_token)
                elif key == "svvv":
                    _append_candidate(svvv_token)
                else:
                    _append_candidate(item.strip())
        else:
            _append_candidate(staking_addr)
            _append_candidate(svvv_token)
        if not candidates:
            _append_candidate(staking_addr)
        return tuple(candidates)
