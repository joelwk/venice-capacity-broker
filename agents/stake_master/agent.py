from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from libs.telemetry.logger import get_logger
from services.staking.client import StakingService


try:
    from libs.telemetry.events import emit as _emit_event
except Exception:  # noqa: BLE001
    def _emit_event(kind: str, payload: dict) -> None:  # type: ignore
        return


_HEARTBEAT_KV_KEY = "staking:heartbeat:last"
_CLAIM_KV_KEY = "staking:claim:last"


logger = get_logger("agent.stake_master")


@dataclass
class StakeMaster:
    staking: StakingService
    heartbeat_interval_hours: float = 48.0
    venice_client: Optional[object] = None
    auto_stake_max_attempts: int = 3
    market: Optional[Any] = None
    _kv_store: Optional[object] = field(default=None, init=False, repr=False)
    _venice_cached: Optional[object] = field(default=None, init=False, repr=False)
    _auto_stake_attempted: bool = field(default=False, init=False, repr=False)
    _auto_stake_attempts: int = field(default=0, init=False, repr=False)
    _last_claim_cached: Optional[float] = field(default=None, init=False, repr=False)

    def run_once(self, live: bool = False) -> Dict[str, Any]:
        """Single heartbeat cycle.

        Reads staking status; if ``live`` and rewards are available, it attempts a
        claim. Returns a structured summary so orchestrators can record the
        outcome without parsing logs.
        """
        status = self.staking.status()
        logger.info(f"Status: {status}")
        _emit_event(
            "staking.status",
            {
                "staked": int(status.get("staked", 0)),
                "rewards": int(status.get("rewards", 0)),
                "active": bool(status.get("active_staker")),
                "cooldown_remaining": status.get("cooldown", {}).get("seconds_remaining"),
            },
        )

        claim_info: Dict[str, Any] = {
            "attempted": bool(live),
            "executed": False,
            "tx": None,
            "reason": None,
        }
        stake_action: Dict[str, Any] = {
            "attempted": False,
            "executed": False,
            "tx": None,
            "reason": None,
        }
        staked_units = int(status.get("staked", 0))
        min_active_units = int(status.get("min_active_stake") or os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0") or 0)
        progressive_env = str(os.getenv("STAKEMASTER_PROGRESSIVE_ENABLE", "true")).strip().lower() in {"1", "true", "yes", "on"}
        try:
            max_attempts_env = os.getenv("STAKEMASTER_AUTO_STAKE_MAX_ATTEMPTS")
            max_attempts = int(max_attempts_env) if max_attempts_env else int(self.auto_stake_max_attempts)
        except Exception:
            max_attempts = int(self.auto_stake_max_attempts)
        max_attempts = max(1, max_attempts)
        stake_action["max_attempts"] = max_attempts
        if live and progressive_env and staked_units <= 0 and min_active_units > 0 and self._auto_stake_attempts < max_attempts:
            available_units = self._wallet_vvv_balance()
            if available_units is not None:
                try:
                    stake_action["available"] = int(available_units)
                except Exception:
                    stake_action["available"] = available_units
            try:
                stake_action["required"] = int(min_active_units)
            except Exception:
                stake_action["required"] = min_active_units
            if available_units is not None and int(available_units) < int(min_active_units):
                available_int = int(available_units)
                required_int = int(min_active_units)
                logger.warning(
                    "Auto-stake skipped: insufficient VVV balance",
                    extra={"available": available_int, "required": required_int},
                )
                stake_action.update({
                    "attempted": False,
                    "executed": False,
                    "reason": "insufficient_balance",
                })
                stake_action["available"] = available_int
                stake_action["required"] = required_int
                try:
                    _emit_event(
                        "staking.auto_stake",
                        {
                            "status": "skipped",
                            "units": required_int,
                            "available": available_int,
                            "reason": "insufficient_balance",
                        },
                    )
                except Exception:
                    pass
                self._auto_stake_attempted = True
                self._auto_stake_attempts = max_attempts
            else:
                nonce_state = self._nonce_state()
                pending_nonce = False
                if nonce_state and int(nonce_state.get("pending", 0)) > int(nonce_state.get("latest", 0)):
                    pending_nonce = True
                    try:
                        logger.warning(
                            "Auto-stake skipped: pending transaction requires nonce clearance",
                            extra={
                                "nonce_latest": nonce_state.get("latest"),
                                "nonce_pending": nonce_state.get("pending"),
                            },
                        )
                    except Exception:
                        pass
                    stake_action.update({
                        "attempted": False,
                        "executed": False,
                        "reason": "pending_nonce",
                        "nonce": nonce_state,
                        "attempts": self._auto_stake_attempts,
                    })
                    _emit_event(
                        "staking.auto_stake",
                        {
                            "status": "skipped",
                            "units": int(min_active_units),
                            "reason": "pending_nonce",
                            "nonce": nonce_state,
                        },
                    )
                    self._auto_stake_attempted = True
                    self._auto_stake_attempts = max_attempts

                if not pending_nonce:
                    stake_action["attempted"] = True
                    try:
                        approve_tx = None
                        try:
                            approve_tx = self.staking.approve(int(min_active_units))
                            stake_action["approve"] = approve_tx
                        except Exception as approve_exc:  # noqa: BLE001
                            logger.warning(f"Auto-stake approve failed: {approve_exc}")
                            stake_action.update({
                                "executed": False,
                                "reason": f"approve_error:{approve_exc}",
                            })
                            _emit_event(
                                "staking.auto_stake",
                                {
                                    "status": "error",
                                    "units": int(min_active_units),
                                    "error": f"approve:{approve_exc}",
                                },
                            )
                            self._auto_stake_attempted = True
                            raise

                        res = self.staking.stake(int(min_active_units))
                        logger.info(f"Auto-stake executed: units={min_active_units} result={res}")
                        _emit_event(
                            "staking.auto_stake",
                            {
                                "status": res.get("status") if isinstance(res, dict) else "ok",
                                "units": int(min_active_units),
                            },
                        )
                        stake_action.update({
                            "executed": True,
                            "tx": res,
                            "reason": "auto_stake",
                            "attempts": self._auto_stake_attempts + 1,
                        })
                        self._auto_stake_attempted = True
                        self._auto_stake_attempts = max_attempts
                        status = self.staking.status()
                        staked_units = int(status.get("staked", staked_units))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Auto-stake attempt failed: {exc}")
                        self._auto_stake_attempts += 1
                        attempt_reason = f"stake_error:{exc}"
                        stake_action.update({
                            "executed": False,
                            "reason": attempt_reason,
                            "attempts": self._auto_stake_attempts,
                        })
                        try:
                            message_lower = str(exc).lower()
                        except Exception:
                            message_lower = ""
                        if any(term in message_lower for term in ["nonce too low", "replacement transaction underpriced"]):
                            nonce_details = nonce_state or self._nonce_state()
                            if nonce_details:
                                stake_action["nonce"] = nonce_details
                            stake_action["followup"] = "nonce_conflict"
                            self._auto_stake_attempted = True
                            self._auto_stake_attempts = max_attempts
                        _emit_event(
                            "staking.auto_stake",
                            {
                                "status": "error",
                                "units": int(min_active_units),
                                "error": str(exc),
                                "attempt": self._auto_stake_attempts,
                                "max_attempts": max_attempts,
                            },
                        )
                        if self._auto_stake_attempts >= max_attempts:
                            self._auto_stake_attempted = True
                        else:
                            self._auto_stake_attempted = False

        elif live and progressive_env and staked_units <= 0 and min_active_units > 0:
            stake_action.update({
                "attempted": False,
                "executed": False,
                "reason": "attempts_exhausted",
                "attempts": self._auto_stake_attempts,
                "max_attempts": max_attempts,
            })

        rewards = int(status.get("rewards", 0))
        claim_info["rewards"] = rewards

        valuation = self._reward_value_summary(rewards)
        if valuation:
            claim_info["valuation"] = valuation

        min_units = self._min_claim_units()
        if min_units > 0:
            claim_info["min_units"] = int(min_units)

        min_interval = self._min_claim_interval_seconds()
        if min_interval > 0:
            claim_info["min_interval_seconds"] = float(min_interval)

        last_claim_ts = self._last_claim_ts()
        if last_claim_ts is not None:
            claim_info["last_claim_ts"] = float(last_claim_ts)

        estimate: Optional[Dict[str, Any]] = None

        if live:
            reason: Optional[str] = None
            now = time.time()

            if rewards <= 0:
                reason = "no_rewards"
            elif min_units > 0 and rewards < min_units:
                reason = "below_min_units"
                claim_info["deficit_units"] = int(min_units - rewards)
            else:
                if min_interval > 0 and last_claim_ts is not None:
                    elapsed = now - last_claim_ts
                    if elapsed < min_interval:
                        reason = "interval_guard"
                        claim_info["cooldown_remaining_seconds"] = max(0.0, float(min_interval - elapsed))

            if reason is None:
                estimate = self._estimate_claim_cost()
                if estimate is None:
                    reason = "missing_gas_estimate"
                else:
                    self._augment_estimate_values(estimate, valuation)
                    claim_info["gas_estimate"] = estimate
                    if self._gas_exceeds_reward(estimate, valuation):
                        reason = "gas_exceeds_reward"

            if reason is None:
                try:
                    res = self.staking.claim()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Claim execution failed: {exc}")
                    claim_info.update({
                        "executed": False,
                        "reason": f"claim_error:{exc}",
                    })
                else:
                    logger.info(f"Claim result: {res}")
                    _emit_event(
                        "staking.claim",
                        {
                            "status": res.get("status"),
                            "tx_hash": res.get("tx_hash"),
                            "rewards": rewards,
                        },
                    )
                    claim_info.update({
                        "executed": True,
                        "tx": res,
                        "reason": "claimed",
                    })
                    self._record_claim_ts(now)
            else:
                if estimate is None and reason not in {"no_rewards", "interval_guard", "below_min_units"}:
                    estimate = self._estimate_claim_cost()
                    if estimate is not None:
                        self._augment_estimate_values(estimate, valuation)
                        claim_info["gas_estimate"] = estimate

                if reason == "no_rewards":
                    logger.info("No rewards to claim (live mode)")
                elif reason == "below_min_units":
                    logger.info(
                        "Claim skipped: rewards below threshold",
                        extra={"rewards": rewards, "min_units": min_units},
                    )
                elif reason == "interval_guard":
                    elapsed = 0.0 if last_claim_ts is None else now - last_claim_ts
                    logger.info(
                        "Claim skipped: minimum interval not met",
                        extra={"elapsed": elapsed, "required": min_interval},
                    )
                elif reason == "gas_exceeds_reward":
                    fee_eth = None if estimate is None else estimate.get("fee_eth")
                    reward_eth = valuation.get("eth") if isinstance(valuation, dict) else None
                    logger.info(
                        "Claim skipped: estimated gas exceeds reward value",
                        extra={
                            "fee_eth": fee_eth,
                            "reward_eth": reward_eth,
                            "fee_usd": None if estimate is None else estimate.get("fee_usd"),
                            "reward_usd": valuation.get("usd") if isinstance(valuation, dict) else None,
                        },
                    )
                elif reason == "missing_gas_estimate":
                    logger.warning("Claim skipped: unable to estimate gas cost")
                else:
                    logger.info(f"Claim skipped: {reason}")
                claim_info["reason"] = reason
        else:
            if rewards > 0:
                estimate = self._estimate_claim_cost()
                if estimate is not None:
                    self._augment_estimate_values(estimate, valuation)
                    claim_info["gas_estimate"] = estimate
            logger.info("Dry-run: would claim if rewards > 0")
            claim_info["reason"] = "dry_run"

        heartbeat_forced = not bool(status.get("active_staker"))
        heartbeat_sent, heartbeat_error = self._ensure_heartbeat(force=heartbeat_forced)
        if heartbeat_forced and not heartbeat_sent and heartbeat_error:
            try:
                logger.warning(
                    "Heartbeat forced but not sent",
                    extra={"reason": heartbeat_error},
                )
            except Exception:
                pass

        return {
            "status": "ok",
            "live": bool(live),
            "snapshot": status,
            "claim": claim_info,
            "stake_action": stake_action,
            "heartbeat": {
                "sent": heartbeat_sent,
                "forced": heartbeat_forced,
                "error": heartbeat_error,
            },
        }

    @staticmethod
    def _parse_int(raw: Optional[str]) -> Optional[int]:
        if raw is None:
            return None
        value = str(raw).strip()
        if value == "":
            return None
        try:
            return int(value, 0)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return None

    def _vvv_decimals(self) -> int:
        raw = os.getenv("VVV_DECIMALS")
        parsed = self._parse_int(raw)
        if parsed is None:
            return 18
        return max(0, parsed)

    def _min_claim_units(self) -> int:
        direct = self._parse_int(os.getenv("STAKEMASTER_MIN_CLAIM_UNITS") or os.getenv("STAKEMASTER_MIN_CLAIM_WEI"))
        if direct is not None:
            return max(0, direct)
        tokens_raw = os.getenv("STAKEMASTER_MIN_CLAIM_TOKENS")
        tokens = None
        if tokens_raw is not None and str(tokens_raw).strip() != "":
            try:
                tokens = float(tokens_raw)
            except Exception:
                tokens = None
        if tokens is None:
            return 0
        scale = 10 ** self._vvv_decimals()
        try:
            return max(0, int(tokens * scale))
        except Exception:
            return 0

    def _min_claim_interval_seconds(self) -> float:
        raw = os.getenv("STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS")
        if raw is None or str(raw).strip() == "":
            default_raw = os.getenv("STAKEMASTER_MIN_CLAIM_INTERVAL_DEFAULT", "3600")
        else:
            default_raw = raw
        try:
            interval = float(default_raw)
        except Exception:
            interval = 3600.0
        return max(0.0, interval)

    def _last_claim_ts(self) -> Optional[float]:
        if self._last_claim_cached is not None:
            return self._last_claim_cached
        store = self._kv()
        if store is None:
            return self._last_claim_cached
        try:
            raw = store.get(_CLAIM_KV_KEY)
            if raw is None:
                return self._last_claim_cached
            ts = float(raw)
        except Exception:
            return self._last_claim_cached
        self._last_claim_cached = ts
        return ts

    def _record_claim_ts(self, ts: float) -> None:
        self._last_claim_cached = float(ts)
        store = self._kv()
        if store is None:
            return
        try:
            store.set(_CLAIM_KV_KEY, str(float(ts)))
        except Exception:
            return

    def _estimate_claim_cost(self) -> Optional[Dict[str, Any]]:
        try:
            return self.staking.estimate_claim_cost()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Claim gas estimate unavailable: {exc}")
            return None

    def _reward_value_summary(self, rewards: int) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"raw": int(rewards)}
        decimals = self._vvv_decimals()
        try:
            scale = float(10 ** decimals)
        except OverflowError:
            scale = float(10**18)
        tokens = None
        if rewards and scale:
            tokens = float(rewards) / scale
        elif rewards == 0:
            tokens = 0.0
        summary["tokens"] = tokens

        price_map: Dict[str, float] = {}
        if self.market is not None:
            try:
                price_map = self.market.prices(["VVV", "ETH"])
            except Exception as exc:  # noqa: BLE001
                try:
                    logger.debug(f"Reward pricing failed: {exc}")
                except Exception:
                    pass
                price_map = {}

        vvv_price_usd = price_map.get("VVV")
        eth_price_usd = price_map.get("ETH")
        if vvv_price_usd is not None:
            summary["price_usd"] = float(vvv_price_usd)
        if eth_price_usd is not None:
            summary["eth_price_usd"] = float(eth_price_usd)
        usd_value = None
        eth_value = None
        if tokens is not None and vvv_price_usd is not None:
            usd_value = tokens * float(vvv_price_usd)
            summary["usd"] = usd_value
        if usd_value is not None and eth_price_usd not in (None, 0.0):
            try:
                eth_value = usd_value / float(eth_price_usd)
                summary["eth"] = eth_value
            except Exception:
                pass
        elif tokens is not None and eth_price_usd is None and vvv_price_usd is None:
            summary["eth"] = None
        return summary

    @staticmethod
    def _augment_estimate_values(estimate: Dict[str, Any], valuation: Dict[str, Any]) -> None:
        fee_wei = estimate.get("fee_wei")
        if fee_wei is None:
            return
        try:
            fee_eth = float(fee_wei) / 1e18
        except Exception:
            fee_eth = None
        if fee_eth is not None:
            estimate["fee_eth"] = fee_eth
        eth_price_usd = valuation.get("eth_price_usd") if isinstance(valuation, dict) else None
        if fee_eth is not None and eth_price_usd not in (None, 0.0):
            try:
                estimate["fee_usd"] = fee_eth * float(eth_price_usd)
            except Exception:
                pass

    @staticmethod
    def _gas_exceeds_reward(estimate: Optional[Dict[str, Any]], valuation: Dict[str, Any]) -> bool:
        if not isinstance(estimate, dict):
            return False
        fee_eth = estimate.get("fee_eth")
        fee_usd = estimate.get("fee_usd")
        reward_eth = valuation.get("eth") if isinstance(valuation, dict) else None
        reward_usd = valuation.get("usd") if isinstance(valuation, dict) else None
        if fee_eth is not None and reward_eth is not None:
            try:
                return float(reward_eth) <= float(fee_eth)
            except Exception:
                pass
        if fee_usd is not None and reward_usd is not None:
            try:
                return float(reward_usd) <= float(fee_usd)
            except Exception:
                pass
        return False

    def _wallet_vvv_balance(self) -> Optional[int]:
        actions = getattr(self.staking, "actions", None)
        if actions is None:
            return None
        erc20 = getattr(actions, "erc20", None)
        if erc20 is None:
            return None
        try:
            from libs.agentkit_ext.agentkit_wallet import get_address

            balance = erc20.functions.balanceOf(get_address()).call()
            return int(balance)
        except Exception as exc:  # noqa: BLE001
            try:
                logger.debug(f"VVV balance lookup failed: {exc}")
            except Exception:
                pass
            return None

    def _nonce_state(self) -> Optional[Dict[str, int]]:
        actions = getattr(self.staking, "actions", None)
        if actions is None:
            return None
        w3 = getattr(actions, "w3", None)
        if w3 is None:
            return None
        try:
            from libs.agentkit_ext.agentkit_wallet import get_address

            address = get_address()
            latest = int(w3.eth.get_transaction_count(address, "latest"))
            pending = int(w3.eth.get_transaction_count(address, "pending"))
            return {"latest": latest, "pending": pending}
        except Exception as exc:  # noqa: BLE001
            try:
                logger.debug(f"Nonce lookup failed: {exc}")
            except Exception:
                pass
            return None

    # --- heartbeat helpers -------------------------------------------------
    def _kv(self):  # lazy to keep Replit/Redis optional
        if self._kv_store is None:
            try:
                from libs.kv.client import KVStore  # type: ignore

                self._kv_store = KVStore()
            except Exception:
                self._kv_store = None
        return self._kv_store

    def _venice(self):  # lazy import to avoid failing when Venice not configured
        if self._venice_cached is not None:
            return self._venice_cached
        if self.venice_client is not None:
            self._venice_cached = self.venice_client
            return self._venice_cached
        try:
            from libs.venice_sdk.client import VeniceClient  # type: ignore

            self._venice_cached = VeniceClient()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Heartbeat Venice client unavailable: {exc}")
            self._venice_cached = None
        return self._venice_cached

    def _should_send_heartbeat(self, now: float, *, force: bool) -> bool:
        if (os.getenv("STAKEMASTER_HEARTBEAT_DISABLE") or "false").strip().lower() in {"1", "true", "yes"}:
            return False
        interval_hours = float(
            os.getenv("STAKEMASTER_HEARTBEAT_INTERVAL_HOURS", str(self.heartbeat_interval_hours))
            or self.heartbeat_interval_hours
        )
        if interval_hours <= 0:
            return False
        store = self._kv()
        if store is None:
            # Without KV we can still best-effort run once per process boot
            return True
        last_raw = store.get(_HEARTBEAT_KV_KEY)
        if force:
            return True
        try:
            last = float(last_raw) if last_raw is not None else 0.0
        except Exception:
            last = 0.0
        return (now - last) >= interval_hours * 3600.0

    def _ensure_heartbeat(self, *, force: bool = False) -> tuple[bool, Optional[str]]:
        now = time.time()
        if not self._should_send_heartbeat(now, force=force):
            return False, None
        client = self._venice()
        if client is None:
            return False, "venice_client_unavailable"
        model = os.getenv("VENICE_HEARTBEAT_MODEL", os.getenv("VENICE_DEFAULT_MODEL", "venice-pro"))
        prompt = os.getenv(
            "STAKEMASTER_HEARTBEAT_PROMPT",
            "Please respond with a single word 'alive'.",
        )
        messages = [
            {"role": "system", "content": "You are a Venice API heartbeat used to retain active staker status."},
            {"role": "user", "content": prompt},
        ]
        try:
            response = client.chat_completions(messages=messages, model=model, temperature=0.0, max_tokens=8)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Heartbeat request failed: {exc}")
            _emit_event("staking.heartbeat", {"status": "error", "error": str(exc)})
            return False, f"request_failed:{type(exc).__name__}"

        store = self._kv()
        if store is not None:
            try:
                store.set(_HEARTBEAT_KV_KEY, str(now))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Failed to persist heartbeat timestamp: {exc}")
        _emit_event(
            "staking.heartbeat",
            {
                "status": "ok",
                "model": model,
                "response_id": response.get("id") if isinstance(response, dict) else None,
            },
        )
        return True, None
