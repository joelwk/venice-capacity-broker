from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


@dataclass
class VeniceConfig:
    base_url: str
    api_key: Optional[str] = None
    # Endpoint paths kept configurable to avoid guessing specifics
    create_root_path: str = "/v1/keys"
    create_subkey_path: str = "/v1/keys/sub"
    revoke_key_path: str = "/v1/keys/{key_id}/revoke"
    challenge_path: str = "/v1/keys/challenge"
    usage_path: str = "/v1/usage"
    rate_limits_path: str = "/v1/rate-limits"
    quota_path: str = "/v1/quota"
    models_path: str = "/v1/models"
    chat_completions_path: str = "/v1/chat/completions"
    vvv_path: str = "/v1/vvv"
    diem_path: str = "/v1/diem"


class VeniceClient:
    """Venice API client with autonomous key and sub-key management.

    Endpoints are configurable via env to match your Venice deployment.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.config = VeniceConfig(
            base_url=base_url or os.getenv("VENICE_API_BASE_URL", "https://api.venice.ai"),
            api_key=api_key or os.getenv("VENICE_API_KEY"),
            create_root_path=os.getenv("VENICE_CREATE_ROOT_PATH", "/v1/keys"),
            create_subkey_path=os.getenv("VENICE_CREATE_SUBKEY_PATH", "/v1/keys/sub"),
            revoke_key_path=os.getenv("VENICE_REVOKE_KEY_PATH", "/v1/keys/{key_id}/revoke"),
            challenge_path=os.getenv("VENICE_CHALLENGE_PATH", "/v1/keys/challenge"),
            usage_path=os.getenv("VENICE_USAGE_PATH", "/v1/usage"),
            rate_limits_path=os.getenv("VENICE_RATE_LIMITS_PATH", "/v1/rate-limits"),
            quota_path=os.getenv("VENICE_QUOTA_PATH", "/v1/quota"),
            models_path=os.getenv("VENICE_MODELS_PATH", "/v1/models"),
            chat_completions_path=os.getenv("VENICE_CHAT_COMPLETIONS_PATH", "/v1/chat/completions"),
            vvv_path=os.getenv("VENICE_VVV_PATH", "/v1/vvv"),
            diem_path=os.getenv("VENICE_DIEM_PATH", "/v1/diem"),
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
        consumption_limit: int,
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "parentKey": parent_key,
            "label": label,
            "consumptionLimit": consumption_limit,
        }
        if expires_at:
            payload["expiresAt"] = expires_at
        return self._post(self.config.create_subkey_path, json=payload)

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
