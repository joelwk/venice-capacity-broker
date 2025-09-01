from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


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
    vvv_path: str = "/vvv"
    diem_path: str = "/diem"


class VeniceClient:
    """Venice API client with autonomous key and sub-key management.

    Endpoints are configurable via env to match your Venice deployment.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.config = VeniceConfig(
            base_url=base_url or os.getenv("VENICE_API_BASE_URL", "https://api.venice.ai/api/v1"),
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
            diem_path=os.getenv("VENICE_DIEM_PATH", "/diem"),
        )

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{path}"

    def _post(self, path: str, json: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.post(self._url(path), json=json, headers=self._headers(), timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Venice error {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def _post_with_key(self, path: str, json: Dict[str, Any], api_key: Optional[str]) -> Dict[str, Any]:
        headers = self._headers()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(self._url(path), json=json, headers=headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Venice error {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = requests.get(self._url(path), params=params or {}, headers=self._headers(), timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Venice error {resp.status_code}: {resp.text}")
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
    def create_root_inference_key(self, wallet_address: str, signature: str, challenge: Optional[str] = None, challenge_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a root inference key by proving wallet control.

        The exact endpoint/payload can be controlled via env paths.
        """
        payload: Dict[str, Any] = {"wallet": wallet_address, "signature": signature}
        if challenge is not None:
            payload["challenge"] = challenge
        if challenge_id is not None:
            payload["challengeId"] = challenge_id
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
        api_key_type = os.getenv("VENICE_API_KEY_TYPE", "READ_ONLY")
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
        return self._post(path, json={})

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

    def get_vvv_signals(self) -> Dict[str, Any]:
        return self._get(self.config.vvv_path)

    def get_diem_signals(self) -> Dict[str, Any]:
        return self._get(self.config.diem_path)
