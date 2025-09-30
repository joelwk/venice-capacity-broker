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


logger = get_logger("agent.stake_master")


@dataclass
class StakeMaster:
    staking: StakingService
    heartbeat_interval_hours: float = 48.0
    venice_client: Optional[object] = None
    _kv_store: Optional[object] = field(default=None, init=False, repr=False)
    _venice_cached: Optional[object] = field(default=None, init=False, repr=False)
    _auto_stake_attempted: bool = field(default=False, init=False, repr=False)

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
        if live and progressive_env and not self._auto_stake_attempted and staked_units <= 0 and min_active_units > 0:
            stake_action["attempted"] = True
            try:
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
                })
                self._auto_stake_attempted = True
                status = self.staking.status()
                staked_units = int(status.get("staked", staked_units))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Auto-stake attempt failed: {exc}")
                stake_action.update({
                    "executed": False,
                    "reason": f"error:{exc}",
                })
                _emit_event(
                    "staking.auto_stake",
                    {
                        "status": "error",
                        "units": int(min_active_units),
                        "error": str(exc),
                    },
                )
                self._auto_stake_attempted = True

        rewards = int(status.get("rewards", 0))

        if live:
            if rewards > 0:
                res = self.staking.claim()
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
            else:
                logger.info("No rewards to claim (live mode)")
                claim_info["reason"] = "no_rewards"
        else:
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
