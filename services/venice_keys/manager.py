from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from libs.venice_sdk.client import VeniceClient


@dataclass
class KeyManager:
    client: VeniceClient

    def issue_root_key(self, wallet_address: str, signature: str) -> dict[str, Any]:
        return self.client.create_root_inference_key(wallet_address, signature)

    def issue_root_key_via_challenge(
        self,
        wallet_address: str,
        signer: Callable[[str], str],
    ) -> dict[str, Any]:
        """Fetch a challenge for wallet, sign it, and create a root key.

        signer: a callable that returns hex signature for the provided message.
        """
        challenge = self.client.get_challenge(wallet_address)
        message = (
            challenge.get("message")
            or challenge.get("challenge")
            or f"Create Venice key for {wallet_address}"
        )
        sig = signer(message)
        return self.client.create_root_inference_key(
            wallet_address,
            sig,
            challenge=challenge.get("challenge"),
            challenge_id=challenge.get("id") or challenge.get("challengeId"),
        )

    def issue_scoped_key(
        self,
        parent_key: str,
        label: str,
        consumption_limit: int | dict[str, Any],
        expires_at: str,
    ) -> dict[str, Any]:
        if expires_at is None or str(expires_at).strip() == "":
            raise ValueError("expires_at is required for scoped sub-keys")
        return self.client.create_scoped_subkey(
            parent_key, label, consumption_limit, expires_at
        )

    def revoke_key(self, key_id: str) -> dict[str, Any]:
        return self.client.revoke_key(key_id)
