from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger("venice_sdk.client")


@dataclass
class VeniceConfig:
    base_url: str
    api_key: str | None = None
    # Endpoint paths kept configurable to adapt to Venice API variants
    # Defaults align with official docs: base_url includes '/api/v1' and paths are root-scoped.
    create_root_path: str = (
        "/api_keys/generate_web3_key"  # GET/POST flow; may be unused by broker
    )
    create_subkey_path: str = (
        "/api_keys"  # POST create key (uses Authorization bearer as parent key)
    )
    revoke_key_path: str = (
        "/api_keys/{key_id}"  # DELETE by id (not all deployments expose)
    )
    challenge_path: str = (
        "/api_keys/generate_web3_key"  # GET returns token for web3 signing
    )
    usage_path: str = "/api_keys/rate_limits/log"
    rate_limits_path: str = "/api_keys/rate_limits"
    quota_path: str = "/api_keys/rate_limits"  # fallback alias
    models_path: str = "/models"
    chat_completions_path: str = "/chat/completions"
    # Legacy aggregate signals (some deployments expose /vvv)
    vvv_path: str = "/vvv"
    # Explicit VVV metrics endpoints (preferred)
    vvv_circ_path: str = "/vvv/circulatingsupply"
    vvv_util_path: str = "/vvv/utilization"
    vvv_yield_path: str = "/vvv/staking_yield"


class VeniceClient:
    """Venice API client with autonomous key and sub-key management.

    Endpoints are configurable via env to match your Venice deployment.
    """

    @staticmethod
    def _normalize_base_url(base: str | None) -> tuple[str, bool]:
        """Ensure the base URL ends with /api/v1, returning (url, coerced_flag)."""
        fallback = "https://api.venice.ai/api/v1"
        value = str(base or "").strip()
        if not value:
            return fallback, True
        normalized = value.rstrip("/")
        if "/api/v1" in normalized:
            idx = normalized.index("/api/v1") + len("/api/v1")
            trimmed = normalized[:idx]
            return trimmed, trimmed != normalized
        coerced = False
        if normalized.endswith("/api"):
            normalized = f"{normalized}/v1"
            coerced = True
        else:
            normalized = f"{normalized}/api/v1"
            coerced = True
        if not normalized.endswith("/api/v1"):
            return fallback, True
        return normalized, coerced

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        raw_base = base_url or os.getenv(
            "VENICE_API_BASE_URL", "https://api.venice.ai/api/v1"
        )
        resolved_base, coerced = self._normalize_base_url(raw_base)
        if coerced:
            logger.warning(
                "VENICE_API_BASE_URL missing '/api/v1'; using %s (was %s)",
                resolved_base,
                raw_base,
            )
        self.config = VeniceConfig(
            base_url=resolved_base,
            api_key=api_key or os.getenv("VENICE_API_KEY"),
            create_root_path=os.getenv(
                "VENICE_CREATE_ROOT_PATH", "/api_keys/generate_web3_key"
            ),
            create_subkey_path=os.getenv("VENICE_CREATE_SUBKEY_PATH", "/api_keys"),
            revoke_key_path=os.getenv("VENICE_REVOKE_KEY_PATH", "/api_keys/{key_id}"),
            challenge_path=os.getenv(
                "VENICE_CHALLENGE_PATH", "/api_keys/generate_web3_key"
            ),
            usage_path=os.getenv("VENICE_USAGE_PATH", "/api_keys/rate_limits/log"),
            rate_limits_path=os.getenv(
                "VENICE_RATE_LIMITS_PATH", "/api_keys/rate_limits"
            ),
            quota_path=os.getenv("VENICE_QUOTA_PATH", "/api_keys/rate_limits"),
            models_path=os.getenv("VENICE_MODELS_PATH", "/models"),
            chat_completions_path=os.getenv(
                "VENICE_CHAT_COMPLETIONS_PATH", "/chat/completions"
            ),
            vvv_path=os.getenv("VENICE_VVV_PATH", "/vvv"),
            vvv_circ_path=os.getenv("VENICE_VVV_CIRC_PATH", "/vvv/circulatingsupply"),
            vvv_util_path=os.getenv("VENICE_VVV_UTIL_PATH", "/vvv/utilization"),
            vvv_yield_path=os.getenv("VENICE_VVV_YIELD_PATH", "/vvv/staking_yield"),
        )

    @classmethod
    def from_env(cls) -> VeniceClient:
        """Create a VeniceClient configured from environment variables."""
        return cls()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _safe_snippet(self, body: str | bytes | None, limit: int = 240) -> str:
        """Return a short, redacted response snippet safe for logs/errors."""
        if body is None:
            return ""
        try:
            text = (
                body.decode("utf-8", errors="replace")
                if isinstance(body, bytes)
                else str(body)
            )
        except Exception:
            text = str(body)
        text = text.replace("\r", " ").replace("\n", " ").strip()
        if len(text) > limit:
            text = text[:limit] + "…"
        try:
            key = self.config.api_key
            if key:
                text = text.replace(key, "[redacted]")
        except Exception:
            pass
        return text

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{path}"

    def _err_hint(self, status: int, body: str) -> str:
        base = self.config.base_url
        if status == 404:
            return (
                f"Endpoint not available (404): {body}. Hint: ensure VENICE_API_BASE_URL includes '/api/v1'"
                f" (got '{base}'). Check Postman docs for valid paths and override VENICE_VVV_*_PATH or key paths as needed."
            )
        if status in (401, 403):
            return (
                f"Venice auth error {status}: {body}. Hint: set a valid VENICE_API_KEY."
            )
        return f"Venice error {status}: {body}"

    def _post(
        self,
        path: str,
        json: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        url = self._url(path)
        try:
            resp = requests.post(url, json=json, headers=headers, timeout=30)
            if not resp.ok:
                error_msg = self._err_hint(
                    resp.status_code, self._safe_snippet(resp.text)
                )
                logger.error(
                    "Venice API POST failed: %s -> %s (status=%d)",
                    url,
                    error_msg,
                    resp.status_code,
                )
                raise RuntimeError(error_msg)
            return resp.json() if resp.content else {}
        except requests.exceptions.RequestException as e:
            logger.error(
                "Venice API POST request exception: %s -> %s",
                url,
                str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Request failed: {e}") from e

    def _post_with_key(
        self,
        path: str,
        json: dict[str, Any],
        api_key: str | None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        url = self._url(path)
        try:
            resp = requests.post(url, json=json, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Request failed: {e}") from e
        if not resp.ok:
            raise RuntimeError(
                self._err_hint(resp.status_code, self._safe_snippet(resp.text))
            )
        return resp.json() if resp.content else {}

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        url = self._url(path)
        try:
            resp = requests.get(url, params=params or {}, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            if path == self.config.vvv_util_path:
                logger.error(
                    "Venice utilization GET request exception: url=%s error=%s",
                    url,
                    str(e),
                    exc_info=True,
                )
            raise RuntimeError(f"Request failed: {e}") from e
        if not resp.ok:
            snippet = self._safe_snippet(resp.text)
            if path == self.config.vvv_util_path:
                logger.error(
                    "Venice utilization GET failed: url=%s status=%d body=%s",
                    url,
                    resp.status_code,
                    snippet,
                )
            raise RuntimeError(self._err_hint(resp.status_code, snippet))
        return resp.json() if resp.content else {}

    def _delete(self, path: str) -> dict[str, Any]:
        url = self._url(path)
        try:
            resp = requests.delete(url, headers=self._headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Request failed: {e}") from e
        if not resp.ok:
            raise RuntimeError(
                self._err_hint(resp.status_code, self._safe_snippet(resp.text))
            )
        return resp.json() if resp.content else {}

    # --- Autonomous key flows ---
    def get_challenge(self, wallet_address: str) -> dict[str, Any]:
        """Obtain a signable challenge for the given wallet.

        By default uses POST to challenge_path with {wallet}. If your deployment
        exposes a GET, override VENICE_CHALLENGE_PATH accordingly.
        """
        try:
            return self._post(
                self.config.challenge_path, json={"wallet": wallet_address}
            )
        except RuntimeError:
            # Fallback to GET if POST not supported
            return self._get(
                self.config.challenge_path, params={"wallet": wallet_address}
            )

    def create_root_inference_key(
        self,
        wallet_address: str,
        signature: str,
        challenge: str | None = None,
        challenge_id: str | None = None,
        api_key_type: str | None = None,
        consumption_limit: dict[str, Any] | int | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a root inference key by proving wallet control.

        The exact endpoint/payload can be controlled via env paths.
        """
        payload: dict[str, Any] = {"wallet": wallet_address, "signature": signature}
        if challenge is not None:
            payload["challenge"] = challenge
        if challenge_id is not None:
            payload["challengeId"] = challenge_id
        if api_key_type is not None:
            payload["apiKeyType"] = api_key_type
        if consumption_limit is not None:
            if isinstance(consumption_limit, dict):
                payload["consumptionLimit"] = dict(consumption_limit)
            else:
                payload["consumptionLimit"] = {"diem": int(consumption_limit)}
        if expires_at is not None:
            payload["expiresAt"] = expires_at
        return self._post(self.config.create_root_path, json=payload)

    def create_scoped_subkey(
        self,
        parent_key: str,
        label: str,
        consumption_limit: int | dict[str, Any],
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a scoped key using the parent_key as Authorization.

        By default posts to '/api_keys' with payload compatible with official docs.
        If 'consumption_limit' is an int, it is treated as DIEM limit.
        """
        if parent_key is None or str(parent_key).strip() == "":
            raise ValueError("parent_key is required to create a scoped subkey")
        if label is None or str(label).strip() == "":
            raise ValueError("label is required to create a scoped subkey")
        if expires_at is None or str(expires_at).strip() == "":
            raise ValueError("expires_at is required for scoped sub-keys (expiresAt)")
        # Determine consumptionLimit shape
        if isinstance(consumption_limit, dict):
            cons = dict(consumption_limit)
        else:
            cons = {"diem": int(consumption_limit)}
        # Venice accepts apiKeyType INFERENCE or ADMIN; default to INFERENCE for chat usage
        api_key_type = os.getenv("VENICE_API_KEY_TYPE", "INFERENCE")
        payload: dict[str, Any] = {
            "apiKeyType": api_key_type,
            "consumptionLimit": cons,
            "description": label,
        }
        payload["expiresAt"] = str(expires_at).strip()
        # Use the parent key as bearer for this call
        return self._post_with_key(
            self.config.create_subkey_path, json=payload, api_key=parent_key
        )

    def revoke_key(self, key_id: str) -> dict[str, Any]:
        path = self.config.revoke_key_path.replace("{key_id}", key_id)
        return self._delete(path)

    # --- Venice key management ---
    def list_api_keys(self) -> dict[str, Any]:
        return self._get("/api_keys")

    def delete_api_key(self, key_id: str) -> dict[str, Any]:
        return self._delete(f"/api_keys/{key_id}")

    # --- Venice usage, limits, models, chat, signals ---
    def get_usage(self, sub_key: str | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if sub_key:
            headers["X-Venice-Sub-Key"] = sub_key
        return self._get(self.config.usage_path, extra_headers=headers or None)

    def get_rate_limits(self, sub_key: str | None = None) -> dict[str, Any]:
        # Prefer rate-limits path; fall back to quota if needed
        headers: dict[str, str] = {}
        if sub_key:
            headers["X-Venice-Sub-Key"] = sub_key
        try:
            return self._get(
                self.config.rate_limits_path, extra_headers=headers or None
            )
        except RuntimeError:
            return self._get(self.config.quota_path, extra_headers=headers or None)

    def list_models(self) -> dict[str, Any]:
        return self._get(self.config.models_path)

    def chat_completions(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        sub_key: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": messages}
        if model:
            payload["model"] = model
        payload.update(extra)
        headers: dict[str, str] = {}
        if sub_key:
            # Use explicit header so parent Authorization can remain the bearer.
            headers["X-Venice-Sub-Key"] = sub_key
        return self._post(
            self.config.chat_completions_path, json=payload, extra_headers=headers
        )

    # --- Signals / metrics ---
    def get_vvv_signals(self) -> dict[str, Any]:
        """Legacy aggregate VVV signals if available (e.g., /vvv)."""
        return self._get(self.config.vvv_path)

    def get_vvv_circulating_supply(self) -> dict[str, Any]:
        return self._get(self.config.vvv_circ_path)

    def get_vvv_utilization(self) -> dict[str, Any]:
        return self._get(self.config.vvv_util_path)

    def get_vvv_staking_yield(self) -> dict[str, Any]:
        return self._get(self.config.vvv_yield_path)

    def get_vvv_metrics(self) -> dict[str, Any]:
        """Fetch all VVV metrics and return a merged dict.

        Returns keys: circulating_supply, utilization, staking_yield
        """

        def _to_float(v: Any) -> float | None:
            try:
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, dict):
                    for k in (
                        "result",
                        "value",
                        "circulatingSupply",
                        "circulating_supply",
                        "utilization",
                        "percentage",
                        "stakingYield",
                        "staking_yield",
                    ):
                        if k in v:
                            return _to_float(v.get(k))
                    return None
                if isinstance(v, str):
                    s = v.strip()
                    if s == "":
                        return None
                    return float(s)
            except Exception:
                return None
            return None

        try:
            circ_raw = self.get_vvv_circulating_supply()
        except Exception:
            circ_raw = {}
        try:
            util_raw = self.get_vvv_utilization()
        except Exception:
            util_raw = {}
        try:
            apy_raw = self.get_vvv_staking_yield()
        except Exception:
            apy_raw = {}

        circ = _to_float(circ_raw)
        util = _to_float(util_raw)
        apy = _to_float(apy_raw)

        # staking_yield payload contains yield + staking totals that can be used to
        # derive per-day per-staked rates for intrinsic VVV FV models.
        emissions_per_day_per_staked: float | None = None
        diem_per_day_per_staked: float | None = None
        try:
            if isinstance(apy_raw, dict):
                total_emission = _to_float(
                    apy_raw.get("totalEmission") or apy_raw.get("total_emission")
                )
                total_staked = _to_float(
                    apy_raw.get("totalStaked") or apy_raw.get("total_staked")
                )
                staker_distribution = _to_float(
                    apy_raw.get("stakerDistribution")
                    or apy_raw.get("staker_distribution")
                )
                if total_staked is not None and total_staked > 0:
                    if total_emission is not None and total_emission > 0:
                        emissions_per_day_per_staked = float(total_emission) / float(
                            total_staked
                        )
                    if staker_distribution is not None and staker_distribution > 0:
                        diem_per_day_per_staked = float(staker_distribution) / float(
                            total_staked
                        )
        except Exception:
            emissions_per_day_per_staked = None
            diem_per_day_per_staked = None
        return {
            "circulating_supply": circ,
            "utilization": util,
            "staking_yield": apy,
            "emissions_vvv_per_day_per_staked_vvv": emissions_per_day_per_staked,
            "diem_per_day_per_staked_vvv": diem_per_day_per_staked,
        }
