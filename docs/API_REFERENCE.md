# Venice Capacity Broker API Reference

**Agent-Optimized API Documentation for Tool Development**

This document provides a complete, structured reference for building autonomous agent tools that interact with the Venice Capacity Broker API, Venice API, and DEX routing utilities.

---

## Table of Contents

1. [API Architecture Overview](#api-architecture-overview)
2. [Base URLs & Configuration](#base-urls--configuration)
3. [Authentication](#authentication)
4. [Broker API Endpoints](#broker-api-endpoints)
5. [Venice API Endpoints](#venice-api-endpoints)
6. [DEX Route Utilities](#dex-route-utilities)
7. [Tool Development Patterns](#tool-development-patterns)
8. [Error Handling & Status Codes](#error-handling--status-codes)
9. [Agent Tool Examples](#agent-tool-examples)

---

## API Architecture Overview

The Venice Capacity Broker system consists of three main API surfaces:

- **Broker API** (`apps/broker_api/`) - FastAPI application for capacity brokerage, tenant management, spot quotes, limit bids, purchases
- **Venice API** (`api.venice.ai/api/v1`) - Upstream Venice AI inference API with model endpoints and key management
- **DEX Routes** (`libs/dex/routes.py`) - On-chain routing utilities for Uniswap V2/V3 path construction

All endpoints follow RESTful conventions with JSON request/response bodies.

---

## Base URLs & Configuration

### Broker API

- **Base URL**: `https://capacity-broker.replit.app` (production) or `http://localhost:8000` (local)
- **API Prefix**: `/v1` for most endpoints
- **OpenAPI Docs**: `/docs` (FastAPI auto-generated)

### Venice API

- **Base URL**: `https://api.venice.ai/api/v1` (must include `/api/v1`)
- **Model Endpoints**: `/chat/completions`, `/models`
- **Signals**: `/vvv/circulatingsupply`, `/vvv/utilization`, `/vvv/staking_yield`

### Environment Variables

For the canonical environment list and mode toggles, see `docs/CONFIGURATION.md`.

```python
VENICE_API_BASE_URL=https://api.venice.ai/api/v1  # Required
VENICE_API_KEY=sk-...                              # Parent key for subkey creation
VENICE_PARENT_KEY=sk-...                           # Alternative to VENICE_API_KEY
BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/...
```

---

## Authentication

### Broker API Authentication

Two authentication modes:

1. **Admin Authentication** (Bearer token)
   - Header: `Authorization: Bearer <BROKER_ADMIN_TOKEN>`
   - Required for: `/v1/admin/*`, `/v1/tenants/*` (admin endpoints)

2. **Tenant Authentication** (Venice subkey)
   - Header: `Authorization: Bearer <VENICE_SUBKEY>`
   - Optional header: `X-Tenant-Id: <tenant_id>`
   - Required for: `/v1/me/*`, `/v1/chat`, tenant-scoped endpoints

### Venice API Authentication

- Header: `Authorization: Bearer <VENICE_API_KEY>`
- Supports parent keys (full access) and subkeys (scoped access)

---

## Broker API Endpoints

### Market Data (`/v1/marketdata`)

#### `GET /v1/env`
Get environment configuration and feature flags.

**Response:**
```json
{
  "version": "0.2.0",
  "features": {
    "quotes": true,
    "purchases": true,
    "bids": false,
    "clearing": false,
    "diem_snapshot_mode": "always"
  },
  "pricing": {
    "discounts": {}
  },
  "payments": {
    "treasury_address": "0x...",
    "accepted_assets": ["USDC", "ETH", "WBTC"],
    "usdc_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  },
  "buyer": {
    "quote_ttl": 120
  },
  "signing": {
    "domain": "Venice Broker",
    "version": "1",
    "chainId": 8453
  }
}
```

`features.bids` is `BIDS_ENABLED`. The buy page shows Order type / Place Bid only when this is true. `signing` is the EIP-712 domain for `PurchaseIntent`.

**Tool Pattern:**
```python
def get_broker_environment() -> dict:
    """Fetch broker environment configuration and feature flags."""
    response = requests.get(f"{BROKER_BASE_URL}/v1/env")
    response.raise_for_status()
    return response.json()
```

---

#### `GET /v1/market/prices`
Get current market prices for symbols.

**Query Parameters:**
- `symbols` (required): Comma-separated list (e.g., `DIEM,ETH,USDC`)

**Response:**
```json
{
  "prices": {
    "DIEM": {"usd": 1.02, "source": "uniswap_v2"},
    "ETH": {"usd": 3245.50, "source": "chainlink"},
    "USDC": {"usd": 1.0, "source": "stablecoin"}
  },
  "meta": {
    "symbols": ["DIEM", "ETH", "USDC"],
    "cacheHit": false,
    "refreshedAt": 1704067200000,
    "cache_hit_rate": 0.85,
    "dex_calls": 3,
    "duration_seconds": 0.12
  }
}
```

**Tool Pattern:**
```python
def get_market_prices(symbols: List[str]) -> dict:
    """Fetch current market prices for cryptocurrency symbols."""
    symbols_str = ",".join(s.upper() for s in symbols)
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/market/prices",
        params={"symbols": symbols_str}
    )
    response.raise_for_status()
    return response.json()
```

---

#### `GET /v1/env-and-prices`
Combined environment status and market prices (optimized for frontend initialization).

**Query Parameters:**
- `symbols` (required): Comma-separated list

**Response:**
```json
{
  "env": { /* same as /v1/env */ },
  "prices": { /* same as /v1/market/prices */ },
  "meta": { /* same as /v1/market/prices meta */ }
}
```

---

### Quotes (`/v1/quotes`)

Spot checkout. Limit bids settle into this same quote shape.

`unitPrice` and `totalPrice` are **minor units of `asset`** (USDC 6 decimals, ETH 18, WBTC 8). Treasury address comes from `GET /v1/env` (`payments.treasury_address`), not from the quote.

Markup is `1 + inventory_utilization * PRICE_UTIL_ALPHA`, then the per-asset discount. Failsafe `hot` returns 503.

#### `GET /v1/quotes`
Generate a quote for purchasing Venice API capacity.

**Query Parameters:**
- `asset` (required): `ETH`, `USDC`, or `WBTC`
- `units` (optional): DIEM credits to buy
- `budget` (optional): Budget in USD (provide `units` or `budget`, not both)

**Response:**
```json
{
  "quoteId": "qM-USDC-1786904894-0.1",
  "units": 0.1,
  "asset": "USDC",
  "unitPrice": 1289058810,
  "totalPrice": 128905881,
  "acceptedMin": 0.01,
  "acceptedMax": 100000.0,
  "expiresAt": 1786905014,
  "discountBps": 500,
  "unitPriceBeforeDiscount": 1356904011
}
```

`unitPrice` 1289058810 USDC-minor is 1289.058810 USDC per DIEM. Changing `units` scales `totalPrice`; it does not change `unitPrice`.

**Tool Pattern:**
```python
def create_quote(asset: str, units: float | None = None, budget_usd: float | None = None) -> dict:
    """Generate a quote for purchasing Venice API capacity."""
    params = {"asset": asset}
    if units:
        params["units"] = units
    if budget_usd:
        params["budget"] = budget_usd
    
    response = requests.get(f"{BROKER_BASE_URL}/v1/quotes", params=params)
    response.raise_for_status()
    return response.json()
```

---

### Purchases (`/v1/purchases`)

#### `GET /v1/purchases/challenge`
Issue a short-lived wallet challenge. Same nonce is used by `POST /verify` and recovery.

**Query Parameters:**
- `txHash` *(required)*
- `buyerAddress` *(required)*

#### `POST /v1/purchases/verify`
Verify a purchase transaction and generate a Venice subkey. Requires a wallet signature over the challenge message.

**Request Body:**
```json
{
  "quoteId": "quote_abc123",
  "txHash": "0x...",
  "buyerAddress": "0x...",
  "signature": "0x...",
  "nonce": "5b8c3a2e0bdc4d3a9f2f6b9b1cf6f28c"
}
```

**Response:**
```json
{
  "purchaseId": "purchase_xyz789",
  "status": "confirmed",
  "quoteId": "quote_abc123",
  "subkey": "sk-venice-...",
  "units": 1.5,
  "asset": "ETH",
  "createdAt": 1704067200
}
```

**Tool Pattern:**
```python
def verify_purchase(quote_id: str, tx_hash: str, buyer_address: str) -> dict:
    """Verify a purchase with a wallet signature, then receive a Venice subkey."""
    challenge = requests.get(
        f"{BROKER_BASE_URL}/v1/purchases/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer_address},
    )
    challenge.raise_for_status()
    payload = challenge.json()
    signature = wallet_personal_sign(payload["message"], buyer_address)
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/purchases/verify",
        json={
            "quoteId": quote_id,
            "txHash": tx_hash,
            "buyerAddress": buyer_address,
            "signature": signature,
            "nonce": payload["nonce"],
        },
    )
    response.raise_for_status()
    return response.json()
```

---

#### `GET /v1/purchases/recover/challenge`
Create a short-lived wallet challenge for recovering a previously purchased key.

**Query Parameters:**
- `txHash` *(required)* — Base transaction hash that funded the treasury.
- `buyerAddress` *(required)* — Wallet address that sent the payment.

**Response:**
```json
{
  "message": "Venice Broker Key Recovery\nTransaction: 0x...\nBuyer: 0x...\nNonce: 5b8c...\nExpires: 2025-11-12T05:30:11Z",
  "nonce": "5b8c3a2e0bdc4d3a9f2f6b9b1cf6f28c",
  "expiresAt": "2025-11-12T05:30:11Z",
  "txHash": "0x...",
  "buyerAddress": "0x..."
}
```

**Notes:**
- The challenge expires 10 minutes after creation and is single-use.
- The wallet must sign the returned `message` with `personal_sign`.

---

#### `POST /v1/purchases/recover`
Recover the API key for a completed purchase using the transaction hash and wallet signature.

**Request Body:**
```json
{
  "txHash": "0x...",
  "buyerAddress": "0x...",
  "signature": "0x...",
  "nonce": "5b8c3a2e0bdc4d3a9f2f6b9b1cf6f28c"
}
```

**Response (key already issued):**
```json
{
  "purchaseId": "purchase_xyz789",
  "status": "fulfilled",
  "tenantId": "w:0x...",
  "subkey": "sk-venice-...",
  "expiresAt": "2025-11-12T05:30:11Z"
}
```

**Response (key issued during recovery):**
```json
{
  "purchaseId": "purchase_xyz789",
  "status": "fulfilled",
  "tenantId": "w:0x...",
  "subkey": "sk-venice-...",
  "expiresAt": "2025-11-12T05:30:11Z"
}
```

**Error Cases:**
- `400` if the challenge expired or the transaction cannot be verified.
- `403` if the signature does not match the buyer wallet.
- `404` if no purchase record matches the given transaction.

**Tool Pattern:**
```python
def recover_purchase_key(tx_hash: str, buyer_address: str) -> dict:
    """Recover a Venice API subkey using a transaction hash + wallet signature."""
    challenge = requests.get(
        f"{BROKER_BASE_URL}/v1/purchases/recover/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer_address}
    )
    challenge.raise_for_status()
    payload = challenge.json()

    message = payload["message"]
    nonce = payload["nonce"]

    signature = wallet_personal_sign(message, buyer_address)

    response = requests.post(
        f"{BROKER_BASE_URL}/v1/purchases/recover",
        json={
            "txHash": tx_hash,
            "buyerAddress": buyer_address,
            "signature": signature,
            "nonce": nonce,
        }
    )
    response.raise_for_status()
    return response.json()
```

---

#### `GET /v1/purchases/{purchase_id}`
Get purchase status by ID. Unauthenticated. Never returns `subkey`; use signed verify/recover to obtain the key.

**Response:**
```json
{
  "purchaseId": "purchase_xyz789",
  "status": "confirmed",
  "quoteId": "quote_abc123",
  "subkey": null,
  "keyIssued": true,
  "units": 1.5,
  "asset": "ETH"
}
```

---

#### `GET /v1/purchases/{purchase_id}/stream`
Stream purchase status updates via Server-Sent Events (SSE). Events omit `subkey` and include `keyIssued`.

**Tool Pattern:**
```python
def stream_purchase_status(purchase_id: str) -> Iterator[dict]:
    """Stream purchase status updates via SSE."""
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/purchases/{purchase_id}/stream",
        stream=True,
        headers={"Accept": "text/event-stream"}
    )
    for line in response.iter_lines():
        if line.startswith(b"data: "):
            yield json.loads(line[6:])
```

---

### Tenants (`/v1/tenants`)

#### `GET /v1/tenants` *(Admin)*
List all tenants.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

**Response:**
```json
[
  {
    "tenantId": "tenant_001",
    "subkey": "sk-venice-...",
    "keyId": "key_abc123",
    "createdAt": 1704067200,
    "brokerLimits": {
      "consumptionLimit": 1000,
      "expiresAt": 1704153600
    }
  }
]
```

---

#### `POST /v1/tenants` *(Admin)*
Create a new tenant with a Venice subkey.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

**Query Parameters:**
- `rotate` (optional): If `true`, rotate subkey if tenant exists
- `revoke_old` (optional): Revoke old key when rotating

**Request Body:**
```json
{
  "tenant_id": "tenant_001",
  "label": "Agent workspace",
  "quota": 1000,
  "expires_at": "2024-01-01T00:00:00Z"
}
```

**Response:**
```json
{
  "id": "tenant_001",
  "label": "Agent workspace",
  "quota": 1000,
  "expires_at": "2024-01-01T00:00:00Z",
  "status": "active"
}
```

**Tool Pattern:**
```python
def create_tenant(
    tenant_id: str,
    label: str,
    quota: int | None = None,
    expires_at: str | None = None,
    admin_token: str | None = None
) -> dict:
    """Create a tenant with scoped Venice API subkey."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    payload = {
        "tenant_id": tenant_id,
        "label": label,
        "quota": quota,
        "expires_at": expires_at
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/tenants",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()
```

---

#### `POST /v1/tenants/{tenant_id}/revoke` *(Admin)*
Revoke a tenant's Venice subkey.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

**Response:**
```json
{
  "success": true,
  "tenantId": "tenant_001",
  "revokedKeyId": "key_abc123"
}
```

---

#### `GET /v1/tenants/{tenant_id}` *(Admin)*
Get tenant details.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

---

#### `GET /v1/tenants/{tenant_id}/usage` *(Admin)*
Get tenant usage statistics.

**Response:**
```json
{
  "tenantId": "tenant_001",
  "totalRequests": 150,
  "totalTokens": 45000,
  "windowStart": 1704067200,
  "windowEnd": 1704153600
}
```

---

#### `GET /v1/tenants/{tenant_id}/broker-limits` *(Admin)*
Get tenant broker limits.

#### `POST /v1/tenants/{tenant_id}/broker-limits` *(Admin)*
Update tenant broker limits.

**Request Body:**
```json
{
  "windowSeconds": 3600,
  "maxRequests": 2000,
  "label": "premium"
}
```

---

### Self-Service (`/v1/me`)

#### `GET /v1/me`
Get current user/tenant identity.

**Headers:**
- `Authorization: Bearer <VENICE_SUBKEY>`
- `X-Tenant-Id: <tenant_id>` (optional)

**Response:**
```json
{
  "role": "tenant",
  "tenantId": "tenant_001",
  "subkey": "sk-venice-..."
}
```

---

#### `GET /v1/me/usage`
Get current tenant's usage statistics.

**Headers:**
- `Authorization: Bearer <VENICE_SUBKEY>`

**Response:**
```json
{
  "tenantId": "tenant_001",
  "totalRequests": 150,
  "totalTokens": 45000
}
```

---

#### `GET /v1/me/broker-limits`
Get current tenant's broker limits.

#### `POST /v1/me/broker-limits`
Update current tenant's broker limits (within admin-defined bounds).

**Request Body:**
```json
{
  "windowSeconds": 3600,
  "maxRequests": 1500,
  "label": "premium"
}
```

---

### Chat Proxy (`/v1/chat`)

#### `POST /v1/chat`
Proxy chat completions to Venice API with tenant-scoped rate limiting.

**Headers:**
- `Authorization: Bearer <VENICE_SUBKEY>`
- `X-Tenant-Id: <tenant_id>` (optional)
- `Idempotency-Key: <key>` (optional, for idempotent requests)

**Request Body:** (OpenAI-compatible)
```json
{
  "model": "venice-1",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "temperature": 0.7,
  "max_tokens": 1000
}
```

**Response:** (OpenAI-compatible)
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1704067200,
  "model": "venice-1",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 12,
    "total_tokens": 22
  }
}
```

**Tool Pattern:**
```python
def chat_completion(
    messages: List[dict],
    model: str = "venice-1",
    temperature: float = 0.7,
    max_tokens: int = 1000,
    subkey: str | None = None,
    tenant_id: str | None = None
) -> dict:
    """Send chat completion request via broker proxy."""
    headers = {}
    if subkey:
        headers["Authorization"] = f"Bearer {subkey}"
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/chat",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()
```

---

### Admin (`/v1/admin`)

#### `GET /v1/admin/quotes`
List recent quotes (admin only).

**Query Parameters:**
- `limit` (optional): Max results (1-500, default: 50)
- `status` (optional): Filter by status

#### `GET /v1/admin/purchases`
List recent purchases (admin only).

**Query Parameters:**
- `limit` (optional): Max results (1-500, default: 50)
- `status` (optional): Filter by status

#### `GET /v1/admin/utilization`
Get utilization statistics (admin only).

**Query Parameters:**
- `minutes` (optional): Time window in minutes (1-10080, default: 1440)

#### `GET /v1/admin/counters`
Get rate limit counters for a tenant (admin only).

**Query Parameters:**
- `tenant_id` (required)
- `scope` (optional): Filter by scope (`chat`, `signals`, etc.)
- `bucket_seconds` (optional): Filter by bucket size
- `limit` (optional): Max results (1-1000, default: 100)

#### `GET /v1/admin/venice/probe`
Probe Venice OpenAPI and detect paths (admin only).

**Query Parameters:**
- `base` (optional): Base host (e.g., `https://api.venice.ai`)
- `timeout` (optional): Timeout in seconds (1.0-60.0, default: 10.0)

---

### Bids (`/v1/bids`) *(Optional Feature)*

Requires `BIDS_ENABLED=1`. Bids and settlement share this flag. Without it these routes 404.

`maxPrice` is a **unit** cap in asset minor units (the buy page field is human units per 1 DIEM). `units` is micro-units (`1_000_000` = 1.0 DIEM). The signature is EIP-712 `PurchaseIntent` over the request fields; it is not a payment.

Statuses include `received`, `in_band`, `accepted_window`, `out_of_band`, `expired`, `filled`.

#### `POST /v1/bids`
Create a bid. Recovers the signer and rejects a buyer/signature mismatch.

**Request Body:**
```json
{
  "buyer": "0x...",
  "units": 100000,
  "maxPrice": 1400000000,
  "asset": "USDC",
  "expiry": 1786905400,
  "slippageBps": 50,
  "nonce": 1786905280000,
  "chainId": 8453,
  "signature": "0x..."
}
```

**Response:**
```json
{
  "bidId": "a1b2c3d4e5f67890",
  "status": "accepted_window"
}
```

---

#### `GET /v1/bids`
List bids for a buyer.

**Query Parameters:**
- `buyer` (required): Wallet address

---

#### `GET /v1/bids/{bid_id}`
Get bid details, including `quoteId` after settle.

---

#### `GET /v1/bids/{bid_id}/stream`
Stream bid status updates via SSE.

---

### Clearing Price (`/v1/pricing`) *(Optional Feature)*

Requires `CLEARING_ENABLED=1` environment variable.

#### `GET /v1/pricing/clearing_price`
Get current clearing price snapshot.

**Response:**
```json
{
  "price": 1356.90,
  "bandMin": 1329.76,
  "bandMax": 1384.04,
  "bandBps": 200,
  "ts": 1786905400
}
```

---

#### `GET /v1/pricing/clearing_price/stream`
Stream clearing price updates via SSE.

---

### Settlement (`/v1/settlement`) *(Optional Feature)*

Requires `BIDS_ENABLED=1` (bids and settlement share this flag). There is no `SETTLEMENT_ENABLED` flag.

#### `POST /v1/settlement/confirm`
Alias for `/v1/purchases/verify` on the purchases router.

#### `POST /v1/settlement/{bid_id}/settle`
If live `unitPrice` ≤ bid `maxPrice`, persist a spot quote, set `Bid.quote_id`, and return that quote. Then pay and call `/v1/purchases/verify` to mint the key and mark the bid `filled`.

**Query Parameters:**
- `asset` (optional): Must match the bid asset if sent

**Response:** same shape as `GET /v1/quotes` (`quoteId`, `units`, `asset`, `unitPrice`, `totalPrice`, `expiresAt`).

**409:** `bid expired`, `bid out of band`, or `price exceeds bid max`. The buy page retries only the last of those for about 30 seconds.

---

#### `GET /v1/settlement/quote`
Preview DEX swap for settlement.

**Query Parameters:**
- `fromToken` (required): ERC-20 address to swap from
- `toAsset` (required): `ETH` or `USDC`
- `amountOut` (required): Desired output amount in minor units (wei or 6dp)
- `path` (optional): CSV path override: `addr0,addr1,[addr2]`

**Response:**
```json
{
  "amountIn": "1000000000000000000",
  "amountOut": "3200000000",
  "path": ["0x...", "0x..."],
  "slippageBps": 50,
  "venues": ["uniswap_v2"]
}
```

---

### Venice Proxy (`/v1/venice`)

#### `POST /v1/venice/web3/challenge` *(Admin)*
Generate Web3 authentication challenge.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

**Request Body:**
```json
{
  "address": "0x..."
}
```

**Response:**
```json
{
  "challenge": "Sign this message..."
}
```

---

#### `POST /v1/venice/web3/create-root-key` *(Admin)*
Create Venice root key via Web3 authentication.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

**Request Body:**
```json
{
  "address": "0x...",
  "signature": "0x...",
  "challenge": "Sign this message..."
}
```

**Response:**
```json
{
  "key": "sk-venice-...",
  "keyId": "key_abc123"
}
```

---

#### `POST /v1/venice/subkey` *(Admin)*
Create a scoped Venice subkey using a parent key.

**Headers:**
- `Authorization: Bearer <BROKER_ADMIN_TOKEN>`

**Request Body:**
```json
{
  "parentKey": "sk-venice-parent-...",
  "label": "Agent workspace",
  "consumptionLimit": 1000,
  "expiresAt": "2024-01-01T00:00:00Z"
}
```

**Response:**
```json
{
  "subkey": "sk-venice-...",
  "keyId": "key_abc123",
  "consumptionLimit": 1000,
  "expiresAt": 1704153600
}
```

**Tool Pattern:**
```python
def create_venice_subkey(
    parent_key: str,
    consumption_limit: int,
    expires_at: int,
    description: str = "",
    admin_token: str | None = None
) -> dict:
    """Create a scoped Venice API subkey."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    payload = {
        "parentKey": parent_key,
        "consumptionLimit": consumption_limit,
        "expiresAt": expires_at,
        "description": description
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/venice/subkey",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()
```

---

## Venice API Endpoints

### Models

#### `GET /models`
List available models.

**Response:**
```json
{
  "data": [
    {
      "id": "venice-1",
      "object": "model",
      "created": 1704067200,
      "owned_by": "venice"
    }
  ]
}
```

---

#### `POST /chat/completions`
Chat completions endpoint (OpenAI-compatible).

**Request Body:**
```json
{
  "model": "venice-1",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "temperature": 0.7,
  "max_tokens": 1000
}
```

**Response:** (OpenAI-compatible format)

---

### Signals & Metrics

#### `GET /vvv/circulatingsupply`
Get VVV circulating supply.

**Response:**
```json
{
  "supply": "1000000000000000000000000",
  "timestamp": 1704067200
}
```

---

#### `GET /vvv/utilization`
Get VVV utilization metrics.

**Response:**
```json
{
  "utilization": 0.75,
  "timestamp": 1704067200
}
```

---

#### `GET /vvv/staking_yield`
Get VVV staking yield.

**Response:**
```json
{
  "apy": 0.14,
  "timestamp": 1704067200
}
```

---

### Key Management

#### `POST /api_keys`
Create a scoped API key.

**Request Body:**
```json
{
  "consumptionLimit": 1000,
  "expiresAt": 1704153600,
  "description": "Agent workspace"
}
```

**Response:**
```json
{
  "key": "sk-venice-...",
  "keyId": "key_abc123"
}
```

---

## DEX Route Utilities

The `libs/dex/routes.py` module provides route planning utilities for Uniswap V2/V3 paths.

### Core Types

#### `RouteHop`
Represents a single hop in a swap route.

```python
@dataclass(frozen=True)
class RouteHop:
    token_in: Address      # Source token address
    token_out: Address     # Destination token address
    fee: Optional[int]     # Fee tier (for Uniswap V3, None for V2)
```

**Normalization:**
- Addresses are normalized to lowercase with `0x` prefix
- Fees must be between 0 and 1,000,000 bps (exclusive)

---

#### `RoutePlan`
Represents a complete swap route with multiple hops.

```python
@dataclass(frozen=True)
class RoutePlan:
    hops: Tuple[RouteHop, ...]
```

**Properties:**
- `tokens`: List of all token addresses in order
- `is_uniswap_v3()`: Returns `True` if any hop has a fee tier
- `reversed()`: Returns a reversed route plan

**Methods:**
- `ensure_v2()`: Raises if route contains fee tiers
- `ensure_v3()`: Raises if route is missing fee tiers
- `with_default_fee(fee: int)`: Fills missing fees with default
- `to_uniswap_v2_path(checksum: bool)`: Converts to V2 path list
- `to_uniswap_v3_path_bytes(reverse: bool)`: Converts to V3 packed bytes

---

### Helper Functions

#### `as_route_plan(route: RouteLike) -> RoutePlan`
Convert a route-like object to a `RoutePlan`.

**Supported Types:**
- `RoutePlan`: Returns as-is
- `Sequence[Address]`: Creates hops from token list

**Example:**
```python
route = as_route_plan(["0x...DIEM", "0x...WETH", "0x...USDC"])
# Creates: RoutePlan([
#   RouteHop("0x...diem", "0x...weth", None),
#   RouteHop("0x...weth", "0x...usdc", None)
# ])
```

---

#### `make_route(tokens: Sequence[Address], fees: Optional[Sequence[Optional[int]]] = None) -> RoutePlan`
Create a route plan from tokens and optional fees.

**Example:**
```python
# Uniswap V2 route
route_v2 = make_route([
    "0x...DIEM",
    "0x...WETH",
    "0x...USDC"
])

# Uniswap V3 route
route_v3 = make_route(
    ["0x...DIEM", "0x...WETH", "0x...USDC"],
    fees=[3000, 500]  # 0.3% and 0.05% fee tiers
)
```

---

### Tool Patterns

#### Build Swap Route
```python
def build_swap_route(
    token_in: str,
    token_out: str,
    intermediate_tokens: List[str] | None = None,
    is_v3: bool = False,
    fees: List[int] | None = None
) -> RoutePlan:
    """Build a swap route for DEX transactions."""
    tokens = [token_in]
    if intermediate_tokens:
        tokens.extend(intermediate_tokens)
    tokens.append(token_out)
    
    if is_v3:
        if fees is None:
            fees = [3000] * (len(tokens) - 1)  # Default 0.3%
        return make_route(tokens, fees)
    else:
        return make_route(tokens)
```

---

#### Reverse Route
```python
def reverse_swap_route(route: RoutePlan) -> RoutePlan:
    """Reverse a swap route (buy -> sell)."""
    return route.reversed()
```

---

#### Encode Route for DEX
```python
def encode_route_for_uniswap_v2(route: RoutePlan) -> List[str]:
    """Encode route as Uniswap V2 path with checksum addresses."""
    return route.to_uniswap_v2_path(checksum=True)

def encode_route_for_uniswap_v3(route: RoutePlan) -> bytes:
    """Encode route as Uniswap V3 packed bytes."""
    return route.to_uniswap_v3_path_bytes(reverse=False)
```

---

## Tool Development Patterns

### 1. Error Handling

All tools should handle HTTP errors gracefully:

```python
import requests
from typing import Optional

def safe_api_call(
    method: str,
    url: str,
    **kwargs
) -> tuple[Optional[dict], Optional[str]]:
    """Make API call with error handling."""
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json(), None
    except requests.HTTPError as e:
        return None, f"HTTP {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return None, f"Error: {str(e)}"
```

---

### 2. Authentication Wrapper

Create a reusable authentication wrapper:

```python
from functools import wraps
from typing import Callable

def with_auth(auth_type: str = "tenant"):
    """Decorator to add authentication headers."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Extract auth token from kwargs or environment
            token = kwargs.pop("auth_token", None) or os.getenv("VENICE_API_KEY")
            if not token:
                raise ValueError(f"{auth_type} authentication required")
            
            headers = kwargs.get("headers", {})
            headers["Authorization"] = f"Bearer {token}"
            kwargs["headers"] = headers
            return func(*args, **kwargs)
        return wrapper
    return decorator

# Usage
@with_auth("tenant")
def get_tenant_info(tenant_id: str, **kwargs) -> dict:
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/tenants/{tenant_id}",
        **kwargs
    )
    response.raise_for_status()
    return response.json()
```

---

### 3. Idempotency

Use idempotency keys for state-changing operations:

```python
import uuid

def create_tenant_with_idempotency(
    tenant_id: str,
    consumption_limit: int,
    expires_at: int,
    idempotency_key: str | None = None
) -> dict:
    """Create tenant with idempotency support."""
    headers = {
        "Authorization": f"Bearer {os.getenv('BROKER_ADMIN_TOKEN')}"
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    else:
        headers["Idempotency-Key"] = str(uuid.uuid4())
    
    payload = {
        "tenantId": tenant_id,
        "consumptionLimit": consumption_limit,
        "expiresAt": expires_at
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/tenants",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()
```

---

### 4. Streaming Responses

Handle Server-Sent Events (SSE) streams:

```python
def stream_updates(endpoint: str, **kwargs) -> Iterator[dict]:
    """Stream SSE updates from an endpoint."""
    headers = kwargs.pop("headers", {})
    headers["Accept"] = "text/event-stream"
    
    response = requests.get(endpoint, stream=True, headers=headers, **kwargs)
    response.raise_for_status()
    
    buffer = ""
    for line in response.iter_lines():
        if line.startswith(b"data: "):
            try:
                data = json.loads(line[6:])
                yield data
            except json.JSONDecodeError:
                continue
```

---

### 5. Rate Limiting

Implement exponential backoff for rate-limited endpoints:

```python
import time
from typing import Optional

def rate_limited_request(
    max_retries: int = 3,
    backoff_factor: float = 2.0,
    initial_delay: float = 1.0
):
    """Decorator for rate-limited requests with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.HTTPError as e:
                    if e.response.status_code == 429:  # Too Many Requests
                        if attempt < max_retries - 1:
                            time.sleep(delay)
                            delay *= backoff_factor
                            continue
                    raise
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        delay *= backoff_factor
                    else:
                        raise
            
            if last_exception:
                raise last_exception
        return wrapper
    return decorator
```

---

## Error Handling & Status Codes

### HTTP Status Codes

- `200 OK`: Success
- `201 Created`: Resource created
- `400 Bad Request`: Invalid request parameters
- `401 Unauthorized`: Missing or invalid authentication
- `403 Forbidden`: Insufficient permissions
- `404 Not Found`: Resource or endpoint not found
- `409 Conflict`: Bid expired, out of band, or live unit price above `maxPrice`; quote already filled
- `429 Too Many Requests`: Rate limit exceeded
- `500 Internal Server Error`: Server error
- `503 Service Unavailable`: Inventory failsafe hot, or market data warming up

### Error Response Format

```json
{
  "detail": "Error message describing what went wrong"
}
```

### Common Error Scenarios

1. **Missing Authentication**
   ```json
   {
     "detail": "VENICE_PARENT_KEY or VENICE_API_KEY must be set"
   }
   ```

2. **Invalid Route**
   ```json
   {
     "detail": "route hops must be contiguous"
   }
   ```

3. **Feature Disabled**
   ```json
   {
     "detail": "purchases disabled"
   }
   ```

4. **Inventory failsafe**
   ```json
   {
     "detail": "inventory failsafe hot: new intake paused"
   }
   ```

5. **Limit bid below market**
   ```json
   {
     "detail": "price exceeds bid max"
   }
   ```

---

## Agent Tool Examples

### Complete Agent Tool Suite

```python
"""
Venice Capacity Broker Agent Tools

This module provides a complete set of tools for autonomous agents
to interact with the Venice Capacity Broker API.
"""

import os
import requests
from typing import List, Optional, Iterator, Dict, Any
from dataclasses import dataclass
from libs.dex.routes import RoutePlan, make_route, as_route_plan


# Configuration
BROKER_BASE_URL = os.getenv("BROKER_BASE_URL", "https://capacity-broker.replit.app")
VENICE_API_BASE_URL = os.getenv("VENICE_API_BASE_URL", "https://api.venice.ai/api/v1")


# ============================================================================
# Market Data Tools
# ============================================================================

def get_broker_environment() -> Dict[str, Any]:
    """Fetch broker environment configuration and feature flags."""
    response = requests.get(f"{BROKER_BASE_URL}/v1/env")
    response.raise_for_status()
    return response.json()


def get_market_prices(symbols: List[str]) -> Dict[str, Any]:
    """Fetch current market prices for cryptocurrency symbols."""
    symbols_str = ",".join(s.upper() for s in symbols)
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/market/prices",
        params={"symbols": symbols_str}
    )
    response.raise_for_status()
    return response.json()


def get_env_and_prices(symbols: List[str]) -> Dict[str, Any]:
    """Fetch combined environment status and market prices."""
    symbols_str = ",".join(s.upper() for s in symbols)
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/env-and-prices",
        params={"symbols": symbols_str}
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# Quotes & Purchases Tools
# ============================================================================

def create_quote(
    asset: str,
    units: Optional[float] = None,
    budget_usd: Optional[float] = None
) -> Dict[str, Any]:
    """Generate a quote for purchasing Venice API capacity."""
    params = {"asset": asset}
    if units:
        params["units"] = units
    if budget_usd:
        params["budget"] = budget_usd
    
    response = requests.get(f"{BROKER_BASE_URL}/v1/quotes", params=params)
    response.raise_for_status()
    return response.json()


def verify_purchase(
    quote_id: str,
    tx_hash: str,
    buyer_address: str
) -> Dict[str, Any]:
    """Verify a purchase with a wallet signature, then receive a Venice subkey."""
    challenge = requests.get(
        f"{BROKER_BASE_URL}/v1/purchases/challenge",
        params={"txHash": tx_hash, "buyerAddress": buyer_address},
    )
    challenge.raise_for_status()
    proof = challenge.json()
    payload = {
        "quoteId": quote_id,
        "txHash": tx_hash,
        "buyerAddress": buyer_address,
        "signature": wallet_personal_sign(proof["message"], buyer_address),
        "nonce": proof["nonce"],
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/purchases/verify",
        json=payload
    )
    response.raise_for_status()
    return response.json()


def get_purchase_status(purchase_id: str) -> Dict[str, Any]:
    """Get purchase status by ID."""
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/purchases/{purchase_id}"
    )
    response.raise_for_status()
    return response.json()


def stream_purchase_status(purchase_id: str) -> Iterator[Dict[str, Any]]:
    """Stream purchase status updates via SSE."""
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/purchases/{purchase_id}/stream",
        stream=True,
        headers={"Accept": "text/event-stream"}
    )
    response.raise_for_status()
    
    for line in response.iter_lines():
        if line.startswith(b"data: "):
            import json
            yield json.loads(line[6:])


# ============================================================================
# Tenant Management Tools
# ============================================================================

def create_tenant(
    tenant_id: str,
    label: str,
    quota: Optional[int] = None,
    expires_at: Optional[str] = None,
    admin_token: Optional[str] = None
) -> Dict[str, Any]:
    """Create a tenant with scoped Venice API subkey."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    payload = {
        "tenant_id": tenant_id,
        "label": label,
        "quota": quota,
        "expires_at": expires_at
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/tenants",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def list_tenants(admin_token: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all tenants (admin only)."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/tenants",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def revoke_tenant_key(
    tenant_id: str,
    admin_token: Optional[str] = None
) -> Dict[str, Any]:
    """Revoke a tenant's Venice subkey (admin only)."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/tenants/{tenant_id}/revoke",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def get_tenant_usage(
    tenant_id: str,
    admin_token: Optional[str] = None
) -> Dict[str, Any]:
    """Get tenant usage statistics (admin only)."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/tenants/{tenant_id}/usage",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# Self-Service Tools
# ============================================================================

def get_current_tenant(subkey: Optional[str] = None) -> Dict[str, Any]:
    """Get current user/tenant identity."""
    headers = {}
    if subkey:
        headers["Authorization"] = f"Bearer {subkey}"
    
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/me",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def get_my_usage(subkey: Optional[str] = None) -> Dict[str, Any]:
    """Get current tenant's usage statistics."""
    headers = {}
    if subkey:
        headers["Authorization"] = f"Bearer {subkey}"
    
    response = requests.get(
        f"{BROKER_BASE_URL}/v1/me/usage",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def update_my_limits(
    window_seconds: Optional[int] = None,
    max_requests: Optional[int] = None,
    label: Optional[str] = None,
    subkey: Optional[str] = None
) -> Dict[str, Any]:
    """Update current tenant's broker limits."""
    headers = {}
    if subkey:
        headers["Authorization"] = f"Bearer {subkey}"
    
    payload = {}
    if window_seconds is not None:
        payload["windowSeconds"] = window_seconds
    if max_requests is not None:
        payload["maxRequests"] = max_requests
    if label is not None:
        payload["label"] = label
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/me/broker-limits",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# Chat Proxy Tools
# ============================================================================

def chat_completion(
    messages: List[Dict[str, str]],
    model: str = "venice-1",
    temperature: float = 0.7,
    max_tokens: int = 1000,
    subkey: Optional[str] = None,
    tenant_id: Optional[str] = None
) -> Dict[str, Any]:
    """Send chat completion request via broker proxy."""
    headers = {}
    if subkey:
        headers["Authorization"] = f"Bearer {subkey}"
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/chat",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# Venice API Tools
# ============================================================================

def list_venice_models(api_key: Optional[str] = None) -> Dict[str, Any]:
    """List available Venice models."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    response = requests.get(
        f"{VENICE_API_BASE_URL}/models",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def get_vvv_circulating_supply(api_key: Optional[str] = None) -> Dict[str, Any]:
    """Get VVV circulating supply."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    response = requests.get(
        f"{VENICE_API_BASE_URL}/vvv/circulatingsupply",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def get_vvv_utilization(api_key: Optional[str] = None) -> Dict[str, Any]:
    """Get VVV utilization metrics."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    response = requests.get(
        f"{VENICE_API_BASE_URL}/vvv/utilization",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def get_vvv_staking_yield(api_key: Optional[str] = None) -> Dict[str, Any]:
    """Get VVV staking yield."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    response = requests.get(
        f"{VENICE_API_BASE_URL}/vvv/staking_yield",
        headers=headers
    )
    response.raise_for_status()
    return response.json()


def create_venice_subkey(
    parent_key: str,
    label: str,
    consumption_limit: int,
    expires_at: Optional[str] = None,
    admin_token: Optional[str] = None
) -> Dict[str, Any]:
    """Create a scoped Venice API subkey via broker."""
    headers = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    
    payload = {
        "parentKey": parent_key,
        "label": label,
        "consumptionLimit": consumption_limit,
        "expiresAt": expires_at
    }
    response = requests.post(
        f"{BROKER_BASE_URL}/v1/venice/subkey",
        json=payload,
        headers=headers
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# DEX Route Tools
# ============================================================================

def build_swap_route(
    token_in: str,
    token_out: str,
    intermediate_tokens: Optional[List[str]] = None,
    is_v3: bool = False,
    fees: Optional[List[int]] = None
) -> RoutePlan:
    """Build a swap route for DEX transactions."""
    tokens = [token_in]
    if intermediate_tokens:
        tokens.extend(intermediate_tokens)
    tokens.append(token_out)
    
    if is_v3:
        if fees is None:
            fees = [3000] * (len(tokens) - 1)  # Default 0.3%
        return make_route(tokens, fees)
    else:
        return make_route(tokens)


def reverse_swap_route(route: RoutePlan) -> RoutePlan:
    """Reverse a swap route (buy -> sell)."""
    return route.reversed()


def encode_route_for_uniswap_v2(route: RoutePlan) -> List[str]:
    """Encode route as Uniswap V2 path with checksum addresses."""
    return route.to_uniswap_v2_path(checksum=True)


def encode_route_for_uniswap_v3(route: RoutePlan) -> bytes:
    """Encode route as Uniswap V3 packed bytes."""
    return route.to_uniswap_v3_path_bytes(reverse=False)


def validate_route(route: RoutePlan) -> tuple[bool, Optional[str]]:
    """Validate a route plan."""
    try:
        route.ensure_v2()  # or ensure_v3() depending on route type
        return True, None
    except ValueError as e:
        return False, str(e)
```

---

## Tool Registration Example (OpenAI Agents SDK)

```python
from openai import OpenAI
from openai.agents import Agent, Tool

# Initialize agent with Venice Broker tools
agent = Agent(
    name="venice_broker_agent",
    instructions="""
    You are an autonomous agent managing Venice API capacity brokerage.
    Use tools to:
    - Monitor market prices and VVV metrics
    - Create quotes and verify purchases
    - Manage tenants and subkeys
    - Execute chat completions via proxy
    - Build DEX swap routes
    """,
    tools=[
        Tool(
            name="get_market_prices",
            function=get_market_prices,
            description="Fetch current market prices for cryptocurrency symbols"
        ),
        Tool(
            name="create_quote",
            function=create_quote,
            description="Generate a quote for purchasing Venice API capacity"
        ),
        Tool(
            name="create_tenant",
            function=create_tenant,
            description="Create a tenant with scoped Venice API subkey"
        ),
        Tool(
            name="chat_completion",
            function=chat_completion,
            description="Send chat completion request via broker proxy"
        ),
        Tool(
            name="build_swap_route",
            function=build_swap_route,
            description="Build a swap route for DEX transactions"
        ),
        # ... add more tools as needed
    ]
)
```

---

## Summary

This API reference provides:

1. **Complete endpoint documentation** for Broker API, Venice API, and DEX routes
2. **Tool development patterns** for error handling, authentication, idempotency, streaming, and rate limiting
3. **Ready-to-use tool examples** for autonomous agents
4. **Agent-friendly structure** with clear descriptions and examples

Agents can use this document to:
- Understand API capabilities and constraints
- Generate tool functions automatically
- Implement robust error handling
- Create composite workflows combining multiple endpoints
- Build DEX routing utilities for on-chain operations

For implementation details, refer to:
- `apps/broker_api/routers/*.py` - FastAPI route handlers
- `libs/dex/routes.py` - DEX route utilities
- `libs/venice_sdk/client.py` - Venice API client

---

**Last Updated:** 2025-01-14  
**API Version:** 0.2.0  
**Documentation Version:** 1.0.0

