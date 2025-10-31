from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TenantCreateRequest(BaseModel):
    tenant_id: str
    label: str
    quota: Optional[int] = None
    expires_at: Optional[str] = None


class TenantResponse(BaseModel):
    id: str
    label: str
    quota: int
    expires_at: Optional[str] = None
    status: str


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    model: Optional[str] = None
    max_tokens: Optional[int] = None


class UsageResponse(BaseModel):
    usage: Dict[str, Any]
    limits: Optional[Dict[str, Any]] = None


class BrokerLimits(BaseModel):
    windowSeconds: Optional[int] = Field(default=None, ge=1)
    maxRequests: Optional[int] = Field(default=None, ge=0)
    label: Optional[str] = None  # classification label (e.g., premium, basic)


class Web3ChallengeRequest(BaseModel):
    wallet: str


class Web3CreateRootRequest(BaseModel):
    address: str
    signature: str
    # Optional pass-throughs if supported by deployment
    challenge: Optional[str] = None
    challengeId: Optional[str] = None
    apiKeyType: Optional[str] = None
    consumptionLimit: Optional[Any] = None
    expiresAt: Optional[str] = None


class CreateSubkeyRequest(BaseModel):
    label: str
    consumptionLimit: Any
    expiresAt: Optional[str] = None
    parentKey: Optional[str] = None  # Optional override; otherwise env is used


class ClearingPriceResponse(BaseModel):
    price: float
    bandMin: float
    bandMax: float
    band: Optional[Dict[str, float]] = None
    change24h: Optional[float] = None
    components: Optional[Dict[str, Any]] = None
    ts: int


class QuoteResponse(BaseModel):
    quoteId: str
    units: float
    asset: str
    unitPrice: int
    totalPrice: int
    acceptedMin: Optional[float] = None
    acceptedMax: Optional[float] = None
    expiresAt: int
    discountBps: Optional[int] = None
    discount: Optional[Dict[str, Any]] = None
    unitPriceBeforeDiscount: Optional[int] = None
    priceHealth: Optional[Dict[str, Any]] = None
    priceGuard: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


class PurchaseVerifyRequest(BaseModel):
    quoteId: str
    txHash: str
    buyerAddress: str
    tenantId: Optional[str] = None
    model: Optional[str] = None


class PurchaseStatus(BaseModel):
    purchaseId: str
    status: str
    tenantId: Optional[str] = None
    subkey: Optional[str] = None
    expiresAt: Optional[str] = None


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
    provider: Optional[str]
    fromToken: str
    toToken: str
    toAsset: str
    path: List[str]
    amountIn: int
    amountOut: int
    expiresAt: int
    approx: bool = False
    slippageBps: Optional[int] = None
    poolTakeBps: Optional[int] = None


__all__ = [
    "TenantCreateRequest",
    "TenantResponse",
    "ChatRequest",
    "UsageResponse",
    "BrokerLimits",
    "Web3ChallengeRequest",
    "Web3CreateRootRequest",
    "CreateSubkeyRequest",
    "ClearingPriceResponse",
    "QuoteResponse",
    "PurchaseVerifyRequest",
    "PurchaseStatus",
    "BidRequest",
    "BidResponse",
    "SettleResponse",
    "DexPreviewResponse",
]
