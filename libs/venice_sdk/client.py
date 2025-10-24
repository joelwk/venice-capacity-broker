from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("venice_sdk.client")

@dataclass
class VeniceConfig:
    base_url: str
    api_key: Optional[str] = None
    # Endpoint paths kept configurable to adapt to Venice API variants
    # Defaults align with official docs: base_url includes '/api/v1' and paths are root-scoped.
    create_root_path: str = "/api_keys/generate_web3_key"  # GET/POST flow; may be unused by broker
    create_subkey_path: str = "/api_keys"  # POST create key (uses Authorization bearer as parent key)
    revoke_key_path: str = "/api_keys/{key_id}"  # DELETE by id (not all deployments expose)
    challenge_path: str = "/api_keys/generate_web3_key"  # GET returns token for web3 signing
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
    def _normalize_base_url(base: Optional[str]) -> tuple[str, bool]:
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

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        raw_base = base_url or os.getenv("VENICE_API_BASE_URL", "https://api.venice.ai/api/v1")
        resolved_base, coerced = self._normalize_base_url(raw_base)
        if coerced:
            logger.warning("VENICE_API_BASE_URL missing '/api/v1'; using %s (was %s)", resolved_base, raw_base)
        self.config = VeniceConfig(
            base_url=resolved_base,
            api_key=api_key or os.getenv("VENICE_API_KEY"),
            create_root_path=os.getenv("VENICE_CREATE_ROOT_PATH", "/api_keys/generate_web3_key"),
            create_subkey_path=os.getenv("VENICE_CREATE_SUBKEY_PATH", "/api_keys"),
            revoke_key_path=os.getenv("VENICE_REVOKE_KEY_PATH", "/api_keys/{key_id}"),
            challenge_path=os.getenv("VENICE_CHALLENGE_PATH", "/api_keys/generate_web3_key"),
            usage_path=os.getenv("VENICE_USAGE_PATH", "/api_keys/rate_limits/log"),
            rate_limits_path=os.getenv("VENICE_RATE_LIMITS_PATH", "/api_keys/rate_limits"),
            quota_path=os.getenv("VENICE_QUOTA_PATH", "/api_keys/rate_limits"),
            models_path=os.getenv("VENICE_MODELS_PATH", "/models"),
            chat_completions_path=os.getenv("VENICE_CHAT_COMPLETIONS_PATH", "/chat/completions"),
            vvv_path=os.getenv("VENICE_VVV_PATH", "/vvv"),
            vvv_circ_path=os.getenv("VENICE_VVV_CIRC_PATH", "/vvv/circulatingsupply"),
            vvv_util_path=os.getenv("VENICE_VVV_UTIL_PATH", "/vvv/utilization"),
            vvv_yield_path=os.getenv("VENICE_VVV_YIELD_PATH", "/vvv/staking_yield"),
        )

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

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
            return f"Venice auth error {status}: {body}. Hint: set a valid VENICE_API_KEY."
        return f"Venice error {status}: {body}"

    def _post(self, path: str, json: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.post(self._url(path), json=json, headers=self._headers(), timeout=30)
        if not resp.ok:
            raise RuntimeError(self._err_hint(resp.status_code, resp.text))
        return resp.json() if resp.content else {}

    def _post_with_key(self, path: str, json: Dict[str, Any], api_key: Optional[str]) -> Dict[str, Any]:
        headers = self._headers()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(self._url(path), json=json, headers=headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(self._err_hint(resp.status_code, resp.text))
        return resp.json() if resp.content else {}
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = requests.get(self._url(path), params=params or {}, headers=self._headers(), timeout=30)
        if not resp.ok:
            raise RuntimeError(self._err_hint(resp.status_code, resp.text))
        return resp.json() if resp.content else {}

    def _delete(self, path: str) -> Dict[str, Any]:
        resp = requests.delete(self._url(path), headers=self._headers(), timeout=30)
        if not resp.ok:
            raise RuntimeError(self._err_hint(resp.status_code, resp.text))
        return resp.json() if resp.content else {}

    # --- Autonomous key flows ---
    def get_challenge(self, wallet_address: str) -> Dict[str, Any]:
        """Obtain a signable challenge for the given wallet.

        By default uses POST to challenge_path with {wallet}. If your deployment
        exposes a GET, override VENICE_CHALLENGE_PATH accordingly.
        """
        try:
            return self._post(self.config.challenge_path, json={"wallet": wallet_address})
        except RuntimeError:
            # Fallback to GET if POST not supported
            return self._get(self.config.challenge_path, params={"wallet": wallet_address})
    def create_root_inference_key(
        self,
        wallet_address: str,
        signature: str,
        challenge: Optional[str] = None,
        challenge_id: Optional[str] = None,
        api_key_type: Optional[str] = None,
        consumption_limit: Optional[Dict[str, Any] | int] = None,
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a root inference key by proving wallet control.

        The exact endpoint/payload can be controlled via env paths.
        """
        payload: Dict[str, Any] = {"wallet": wallet_address, "signature": signature}
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
        consumption_limit: int | Dict[str, Any],
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a scoped key using the parent_key as Authorization.

        By default posts to '/api_keys' with payload compatible with official docs.
        If 'consumption_limit' is an int, it is treated as DIEM limit.
        """
        # Determine consumptionLimit shape
        if isinstance(consumption_limit, dict):
            cons = dict(consumption_limit)
        else:
            cons = {"diem": int(consumption_limit)}
        # Venice accepts apiKeyType INFERENCE or ADMIN; default to INFERENCE for chat usage
        api_key_type = os.getenv("VENICE_API_KEY_TYPE", "INFERENCE")
        payload: Dict[str, Any] = {
            "apiKeyType": api_key_type,
            "consumptionLimit": cons,
            "description": label,
        }
        if expires_at:
            payload["expiresAt"] = expires_at
        # Use the parent key as bearer for this call
        return self._post_with_key(self.config.create_subkey_path, json=payload, api_key=parent_key)

    def revoke_key(self, key_id: str) -> Dict[str, Any]:
        path = self.config.revoke_key_path.replace("{key_id}", key_id)
        return self._delete(path)

    # --- Venice key management ---
    def list_api_keys(self) -> Dict[str, Any]:
        return self._get("/api_keys")

    def delete_api_key(self, key_id: str) -> Dict[str, Any]:
        return self._delete(f"/api_keys/{key_id}")

    # --- Venice usage, limits, models, chat, signals ---
    def get_usage(self) -> Dict[str, Any]:
        return self._get(self.config.usage_path)

    def get_rate_limits(self) -> Dict[str, Any]:
        # Prefer rate-limits path; fall back to quota if needed
        try:
            return self._get(self.config.rate_limits_path)
        except RuntimeError:
            return self._get(self.config.quota_path)

    def list_models(self) -> Dict[str, Any]:
        return self._get(self.config.models_path)

    def chat_completions(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"messages": messages}
        if model:
            payload["model"] = model
        payload.update(extra)
        return self._post(self.config.chat_completions_path, json=payload)

    # --- Signals / metrics ---
    def get_vvv_signals(self) -> Dict[str, Any]:
        """Legacy aggregate VVV signals if available (e.g., /vvv)."""
        return self._get(self.config.vvv_path)

    def get_vvv_circulating_supply(self) -> Dict[str, Any]:
        return self._get(self.config.vvv_circ_path)

    def get_vvv_utilization(self) -> Dict[str, Any]:
        return self._get(self.config.vvv_util_path)

    def get_vvv_staking_yield(self) -> Dict[str, Any]:
        return self._get(self.config.vvv_yield_path)

    def get_vvv_metrics(self) -> Dict[str, Any]:
        """Fetch all VVV metrics and return a merged dict.

        Returns keys: circulating_supply, utilization, staking_yield
        """
        def _to_float(v: Any) -> Optional[float]:  # noqa: ANN401
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
        return {
            "circulating_supply": circ,
            "utilization": util,
            "staking_yield": apy,
        }
