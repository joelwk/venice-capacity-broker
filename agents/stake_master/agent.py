from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

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

    def run_once(self, live: bool = False) -> None:
        """Single heartbeat.

        Reads status; if live is True and rewards>0, attempts a claim.
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
        if live:
            if int(status.get("rewards", 0)) > 0:
                res = self.staking.claim()
                logger.info(f"Claim result: {res}")
                _emit_event(
                    "staking.claim",
                    {
                        "status": res.get("status"),
                        "tx_hash": res.get("tx_hash"),
                        "rewards": int(status.get("rewards", 0)),
                    },
                )
            else:
                logger.info("No rewards to claim (live mode)")
        else:
            logger.info("Dry-run: would claim if rewards > 0")
        self._ensure_heartbeat(force=not bool(status.get("active_staker")))

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

    def _ensure_heartbeat(self, *, force: bool = False) -> None:
        now = time.time()
        if not self._should_send_heartbeat(now, force=force):
            return
        client = self._venice()
        if client is None:
            return
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
            return

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
