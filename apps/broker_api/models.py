from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TenantCreateRequest(BaseModel):
    tenant_id: str
    label: str
    quota: int | None = None
    expires_at: str | None = None


class TenantResponse(BaseModel):
    id: str
    label: str
    quota: int
    expires_at: str | None = None
    status: str


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    model: str | None = None
    max_tokens: int | None = None


class UsageResponse(BaseModel):
    usage: dict[str, Any]
    limits: dict[str, Any] | None = None


class BrokerLimits(BaseModel):
    windowSeconds: int | None = Field(default=None, ge=1)
    maxRequests: int | None = Field(default=None, ge=0)
    label: str | None = None  # classification label (e.g., premium, basic)


class Web3ChallengeRequest(BaseModel):
    wallet: str


class Web3CreateRootRequest(BaseModel):
    address: str
    signature: str
    # Optional pass-throughs if supported by deployment
    challenge: str | None = None
    challengeId: str | None = None
    apiKeyType: str | None = None
    consumptionLimit: Any | None = None
    expiresAt: str | None = None


class CreateSubkeyRequest(BaseModel):
    label: str
    consumptionLimit: Any
    expiresAt: str | None = None
    parentKey: str | None = None  # Optional override; otherwise env is used


class ClearingPriceResponse(BaseModel):
    price: float
    bandMin: float
    bandMax: float
    band: dict[str, float] | None = None
    change24h: float | None = None
    components: dict[str, Any] | None = None
    ts: int


class QuoteResponse(BaseModel):
    quoteId: str
    units: float
    asset: str
    unitPrice: int
    totalPrice: int
    acceptedMin: float | None = None
    acceptedMax: float | None = None
    expiresAt: int
    discountBps: int | None = None
    discount: dict[str, Any] | None = None
    unitPriceBeforeDiscount: int | None = None
    priceHealth: dict[str, Any] | None = None
    priceGuard: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class PurchaseVerifyRequest(BaseModel):
    quoteId: str
    txHash: str
    buyerAddress: str
    # Wallet proof: nonce from /v1/purchases/challenge, personal_sign signature.
    signature: str | None = None
    nonce: str | None = None
    tenantId: str | None = None
    model: str | None = None


class PurchaseStatus(BaseModel):
    purchaseId: str
    status: str
    tenantId: str | None = None
    subkey: str | None = None
    keyIssued: bool | None = None
    expiresAt: str | None = None


class PurchaseRecoverChallenge(BaseModel):
    message: str
    nonce: str
    expiresAt: str
    txHash: str
    buyerAddress: str


class PurchaseRecoverRequest(BaseModel):
    txHash: str
    buyerAddress: str
    signature: str
    nonce: str


class BidRequest(BaseModel):
    buyer: str
    units: int  # integer micro-units (1e6 = 1.0 unit)
    maxPrice: int
    asset: str
    expiry: int
    slippageBps: int
    nonce: int
    chainId: int
    signature: str


class BidResponse(BaseModel):
    bidId: str
    status: str


class SettleResponse(BaseModel):
    quoteId: str
    units: float
    asset: str
    unitPrice: int
    totalPrice: int
    expiresAt: int


class DexPreviewResponse(BaseModel):
    provider: str | None
    fromToken: str
    toToken: str
    toAsset: str
    path: list[str]
    amountIn: int
    amountOut: int
    expiresAt: int
    approx: bool = False
    slippageBps: int | None = None
    poolTakeBps: int | None = None


__all__ = [
    "BidRequest",
    "BidResponse",
    "BrokerLimits",
    "ChatRequest",
    "ClearingPriceResponse",
    "CreateSubkeyRequest",
    "DexPreviewResponse",
    "PurchaseRecoverChallenge",
    "PurchaseRecoverRequest",
    "PurchaseStatus",
    "PurchaseVerifyRequest",
    "QuoteResponse",
    "SettleResponse",
    "TenantCreateRequest",
    "TenantResponse",
    "UsageResponse",
    "Web3ChallengeRequest",
    "Web3CreateRootRequest",
]
