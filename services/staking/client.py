from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from libs.agentkit_ext.actions import VVVActions
from libs.agentkit_ext.agentkit_wallet import get_address

try:
    from web3 import Web3  # type: ignore
except Exception:  # pragma: no cover - web3 optional in tests
    Web3 = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

_PANIC_0X11_RE = re.compile(r"panic(?:\s+error|\s+code)?[\s(]*0x11", re.IGNORECASE)
_PANIC_SELECTOR_RE = re.compile(r"(?:0x)?4e487b71([0-9a-fA-F]{64})")
_ARITH_OVERFLOW_RE = re.compile(
    r"arithmetic\s+overflow|overflow/underflow", re.IGNORECASE
)


def stake_estimate_error_signature(exc: Exception) -> str | None:
    """Return a stable signature for stake/approve gas estimate failures."""

    try:
        message = str(exc)
    except Exception:
        message = ""
    if not message:
        return None

    if _PANIC_0X11_RE.search(message) is not None:
        return "panic_0x11"
    if _ARITH_OVERFLOW_RE.search(message) is not None:
        return "panic_0x11"

    try:
        match = _PANIC_SELECTOR_RE.search(message)
        if match is not None:
            code = int(match.group(1), 16)
            if code == 0x11:
                return "panic_0x11"
            return f"panic_{hex(code)}"
    except Exception:
        pass

    return None


def is_stake_estimate_overflow_error(exc: Exception) -> bool:
    """Return True when an estimate failure is a Panic(0x11) overflow/underflow."""

    return stake_estimate_error_signature(exc) == "panic_0x11"


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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        return float(str(raw))
    except Exception:
        return float(default)


def stake_overflow_backoff_policy(
    *, max_retries: int | None = None
) -> tuple[int, float, int]:
    """Return the overflow backoff policy shared across staking agents."""

    configured_max = _env_int(
        "STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES",
        _env_int("STAKEMASTER_STAKE_OVERFLOW_MAX_RETRIES", 4),
    )
    configured_max = max(0, int(configured_max))
    retries = configured_max if max_retries is None else max(0, int(max_retries))

    backoff_mult = _env_float(
        "STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT",
        _env_float("STAKEMASTER_STAKE_OVERFLOW_BACKOFF_MULT", 0.5),
    )
    if not (0.0 < float(backoff_mult) < 1.0):
        backoff_mult = 0.5

    min_units = _env_int(
        "STAKEMASTER_IDLE_STAKE_OVERFLOW_MIN_UNITS",
        _env_int("STAKEMASTER_STAKE_OVERFLOW_MIN_UNITS", 1),
    )
    min_units = max(1, int(min_units))
    return int(retries), float(backoff_mult), int(min_units)


def run_with_stake_overflow_backoff(
    attempt: Callable[[int], Any],
    units: int,
    *,
    max_retries: int | None = None,
    stop_if: Callable[[int], str | None] | None = None,
) -> tuple[Any | None, int, list[dict[str, Any]], str | None]:
    """Run ``attempt`` with overflow-driven size backoff.

    Returns: (result, attempted_units, attempts, stop_reason)
    """

    if units <= 0:
        return None, 0, [], "zero_units"

    retries, backoff_mult, min_units = stake_overflow_backoff_policy(
        max_retries=max_retries
    )

    attempts: list[dict[str, Any]] = []
    current = int(units)
    if current < min_units:
        return None, int(current), [], "below_min_units"

    stop_reason: str | None = None

    for attempt_idx in range(int(retries) + 1):
        if stop_if is not None:
            reason = stop_if(int(current))
            if reason:
                stop_reason = str(reason)
                break
        try:
            result = attempt(int(current))
            return result, int(current), attempts, None
        except Exception as exc:
            error_signature = stake_estimate_error_signature(exc) or "unknown"
            if not is_stake_estimate_overflow_error(exc):
                raise
            attempts.append(
                {
                    "attempt_idx": int(attempt_idx),
                    "units": int(current),
                    "error_signature": str(error_signature),
                    "error": str(exc),
                }
            )
            if attempt_idx >= retries:
                stop_reason = "retries_exhausted"
                break
            next_units = int(float(current) * float(backoff_mult))
            if next_units >= current:
                next_units = current - 1
            if next_units < min_units:
                stop_reason = "below_min_units"
                break
            current = next_units

    if stop_reason is None:
        stop_reason = "unknown"
    return None, int(current), attempts, str(stop_reason)


@dataclass
class StakingService:
    """Wrapper around staking actions (replace with on-chain calls)."""

    actions: VVVActions
    _status_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _recent_snapshots: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=3), init=False, repr=False
    )
    _last_snapshot: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def approve(self, amount: int) -> dict[str, Any]:
        return self.actions.approve(amount)

    def stake(
        self, amount: int, *, gas_overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.actions.stake(amount, gas_overrides=gas_overrides)

    def estimate_stake(self, amount: int) -> dict[str, Any]:
        estimator = getattr(self.actions, "estimate_stake", None)
        if estimator is None:
            raise RuntimeError("stake_estimator_unavailable")
        return estimator(int(amount))

    def claim(self) -> dict[str, Any]:
        return self.actions.claim()

    def estimate_claim_cost(self) -> dict[str, Any] | None:
        estimator = getattr(self.actions, "estimate_claim_cost", None)
        if estimator is None:
            return None
        try:
            return estimator()
        except Exception as exc:
            raise RuntimeError(f"claim_cost_estimate_failed:{exc}") from exc

    def unstake(self, amount: int) -> dict[str, Any]:
        return self.actions.unstake(amount)

    def status(self) -> dict[str, Any]:
        """Best-effort read of staking status without relying on full ABI.

        Tries common patterns:
        - balanceOf(address) / stakes(address) for staked balance
        - earned(address) / claimable(address) / pendingRewards(address) for rewards
        Falls back to the last known-good snapshot when RPC reads fail.
        """
        if Web3 is None:
            raise RuntimeError(
                "web3 library not available; staking status requires web3"
            )

        with self._status_lock:
            w3 = self.actions.w3
            staking_addr = Web3.to_checksum_address(self.actions.staking.address)  # type: ignore[attr-defined]
            wallet = Web3.to_checksum_address(get_address())
            addr_arg = bytes.fromhex(wallet[2:])

            status_label = "ok"
            staked_meta: dict[str, Any] | None = None
            fallback_used = False

            try:
                staked_value, staked_meta = self._staked_amount(
                    wallet, staking_addr, addr_arg
                )
                missing_stake = staked_meta is None and staked_value == 0
                staked = None if missing_stake else int(staked_value)
                if missing_stake:
                    status_label = "unknown"
            except Exception:
                staked = None
                staked_meta = None
                status_label = "unknown"

            try:
                rewards_value = self._reward_amount(wallet, staking_addr, addr_arg)
            except Exception:
                rewards_value = None

            cooldown_seconds = int(
                os.getenv("VVV_COOLDOWN_SECONDS", str(7 * 24 * 60 * 60))
            )

            def _call_timestamp(signatures: list[str]) -> int | None:
                for sig in signatures:
                    try:
                        ts = self._call_raw_uint(staking_addr, wallet, addr_arg, sig)
                        if ts and 0 < ts < 10_000_000_000:
                            return ts
                    except Exception:
                        continue
                return None

            cooldown_end = _call_timestamp(
                [
                    "cooldownEndsAt(address)",
                    "withdrawableTimestamp(address)",
                    "cooldowns(address)",
                ]
            )
            now = int(time.time())
            cooldown_remaining = (
                (cooldown_end - now) if cooldown_end and cooldown_end > now else None
            )

            min_active = int(os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0") or 0)

            if staked is None:
                last_snapshot = self._last_snapshot or {}
                if last_snapshot:
                    fallback_used = True
                    staked = int(last_snapshot.get("staked") or 0)
                    if rewards_value is None:
                        rewards_value = int(last_snapshot.get("rewards") or 0)
                else:
                    staked = 0
            rewards = int(rewards_value or 0)

            active_staker = bool(staked > max(0, min_active))
            if fallback_used and self._last_snapshot:
                active_staker = bool(
                    self._last_snapshot.get("active_staker", active_staker)
                )

            snapshot_source = "last_known" if fallback_used else "live"
            snapshot_status = (
                status_label if status_label != "ok" or fallback_used else "ok"
            )
            snapshot = {
                "status": snapshot_status,
                "chain_id": w3.eth.chain_id,
                "wallet": wallet,
                "staking_contract": staking_addr,
                "staked": int(staked),
                "staked_source": (
                    staked_meta
                    if staked_meta
                    else ({"source": "last_known"} if fallback_used else None)
                ),
                "rewards": rewards,
                "unclaimed_rewards": rewards,
                "active_staker": active_staker,
                "min_active_stake": min_active,
                "snapshot_source": snapshot_source,
                "cooldown": {
                    "configured_seconds": cooldown_seconds,
                    "ends_at": cooldown_end,
                    "seconds_remaining": cooldown_remaining,
                },
            }
            self._record_snapshot(snapshot)
            return snapshot

    def _record_snapshot(self, snapshot: dict[str, Any]) -> None:
        try:
            entry = dict(snapshot)
        except Exception:
            return
        entry["ts"] = time.time()
        self._recent_snapshots.append(entry)
        if entry.get("status") == "ok":
            self._last_snapshot = entry

    def is_active_staker(self, status: dict[str, Any] | None = None) -> bool:
        """Return True when staking position qualifies as active for Venice rewards."""

        snapshot = status or self.status()
        if snapshot.get("status") != "ok":
            return False
        staked = int(snapshot.get("staked") or 0)
        min_active = int(
            snapshot.get("min_active_stake")
            or os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0")
            or 0
        )
        return staked > max(0, int(min_active))

    # ------------------------------------------------------------------
    def _is_plausible_stake(self, value: int) -> bool:
        """Sanity-check staked amount to avoid mis-parsing tuple outputs."""

        if value <= 0:
            return False
        sanity_flag = (os.getenv("STAKEMASTER_STAKE_SANITY") or "1").strip().lower()
        if sanity_flag in {"0", "false", "off"}:
            return True
        try:
            max_tokens = float(os.getenv("STAKEMASTER_STAKE_MAX_TOKENS", "1000000"))
        except Exception:
            max_tokens = 1_000_000.0
        try:
            decimals = int(os.getenv("VVV_DECIMALS") or 18)
        except Exception:
            decimals = 18
        try:
            tokens = float(value) / float(10 ** max(decimals, 0))
        except Exception:
            tokens = float(value)
        if max_tokens > 0 and tokens > max_tokens:
            return False
        last_tokens = None
        try:
            if self._last_snapshot:
                last_tokens = float(self._last_snapshot.get("staked") or 0) / float(
                    10 ** max(decimals, 0)
                )
        except Exception:
            last_tokens = None
        if last_tokens and last_tokens > 0 and tokens > last_tokens * 50:
            return False
        return True

    def _staked_amount(
        self,
        wallet: str,
        staking_addr: str,
        wallet_arg: bytes,
    ) -> tuple[int, dict[str, Any] | None]:
        w3 = self.actions.w3
        codec = getattr(w3, "codec", None)
        zero_candidate: tuple[int, dict[str, Any]] | None = None

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
                meta = {
                    "target": target,
                    "signature": sig,
                    "outputs": outputs,
                    "select": select,
                }
                if value > 0:
                    if self._is_plausible_stake(int(value)):
                        return int(value), meta
                    _logger.warning(
                        "Ignoring implausible stake read %s from %s; trying next signature",
                        value,
                        sig,
                    )
                    continue
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

    def _call_raw_uint(
        self, target: str, wallet: str, wallet_arg: bytes, signature: str
    ) -> int | None:
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
    def _encode_signature(signature: str, wallet_arg: bytes) -> bytes | None:
        if Web3 is None:
            return None
        sig = signature.strip()
        if not sig:
            return None
        if "(address)" in sig:
            return Web3.keccak(text=sig)[:4] + wallet_arg.rjust(32, b"\x00")
        return Web3.keccak(text=sig)[:4]

    def _decode_uint(self, raw: Any) -> int | None:
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
    ) -> int | None:
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

    def _staked_call_specs(self) -> Sequence[tuple[str, Sequence[str], int]]:
        raw = os.getenv("STAKEMASTER_STAKED_SIGNATURES")
        entries: Sequence[str]
        if raw:
            entries = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            entries = (
                "balanceOf(address)",
                "balanceOfUnlocked(address)",
                "stakes(address)|uint256,uint256|0",
                "staked(address)",
                "userInfo(address)|uint256,uint256|0",
            )
        specs: list[tuple[str, Sequence[str], int]] = []
        for entry in entries:
            spec = self._parse_spec_entry(entry)
            if spec is not None:
                specs.append(spec)
        if not specs:
            specs.append(("balanceOf(address)", ("uint256",), 0))
        return specs

    @staticmethod
    def _parse_spec_entry(entry: str) -> tuple[str, Sequence[str], int] | None:
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

        def _append_candidate(value: str | None) -> None:
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
