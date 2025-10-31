from __future__ import annotations

import os
from typing import Callable, Optional

from fastapi import APIRouter, Header, HTTPException

from libs.venice_sdk.client import VeniceClient

from ..models import CreateSubkeyRequest, Web3ChallengeRequest, Web3CreateRootRequest

router = APIRouter()

_client: VeniceClient
_require_admin: Callable[[Optional[str]], None]


def init_router(*, client: VeniceClient, require_admin: Callable[[Optional[str]], None]) -> APIRouter:
    global _client, _require_admin

    _client = client
    _require_admin = require_admin

    return router


@router.post("/v1/venice/web3/challenge")
def venice_web3_challenge(
    req: Web3ChallengeRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict:
    _require_admin(authorization)
    try:
        return _client.get_challenge(req.wallet)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"challenge fetch failed: {exc}") from exc


@router.post("/v1/venice/web3/create-root-key")
def venice_web3_create_root(
    req: Web3CreateRootRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict:
    _require_admin(authorization)
    try:
        return _client.create_root_inference_key(
            wallet_address=req.address,
            signature=req.signature,
            challenge=req.challenge,
            challenge_id=req.challengeId,
            api_key_type=req.apiKeyType,
            consumption_limit=req.consumptionLimit,
            expires_at=req.expiresAt,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"root key creation failed: {exc}") from exc


@router.post("/v1/venice/subkey")
def venice_create_subkey(
    req: CreateSubkeyRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict:
    """Admin-only: create a scoped subkey using a parent key."""
    _require_admin(authorization)
    parent = req.parentKey or os.getenv("VENICE_PARENT_KEY") or os.getenv("VENICE_API_KEY")
    if not parent:
        raise HTTPException(status_code=400, detail="no parent key provided and VENICE_PARENT_KEY/VENICE_API_KEY not set")
    try:
        return _client.create_scoped_subkey(
            parent_key=parent,
            label=req.label,
            consumption_limit=req.consumptionLimit,
            expires_at=req.expiresAt,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"subkey creation failed: {exc}") from exc


__all__ = ["router", "init_router"]
